import time

from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ai_engine.gateway import generate
from ai_engine.gateway.budget import prepare_input
from ai_engine.gateway.config import GatewayError
from ai_engine.retrieval import search
from ai_engine.retrieval.options import RetrievalOptions
from apps.documents.selectors import active_chunks, documents
from apps.monitoring.metering import QuotaExceeded
from apps.users.models import User
from apps.users.permissions import require_capability
from common.context import RequestContext

from .configuration import runtime_signature
from .corpus import verify_frozen
from .metrics import aggregate, citation_metrics, retrieval_metrics
from .models import EvalResult, EvalRun


def case_context(run, case):
    initiator = User.objects.select_related("tenant").get(
        pk=run.actor_id, tenant_id=run.tenant_id, is_active=True, tenant__is_active=True
    )
    require_capability(RequestContext.for_user(initiator, run.trace_id), "evaluation.run")
    ctx = RequestContext.for_user(initiator, f"eval-{run.pk.hex[:16]}-{(case.case_key or case.pk.hex)[:25]}")
    if not case.actor_fixture:
        return ctx
    fixture_id = run.dataset.manifest.get("actors", {}).get(case.actor_fixture)
    if not fixture_id or (not initiator.is_tenant_admin and str(run.actor_id) != fixture_id):
        raise ValueError("EVALUATION_ACTOR_NOT_ALLOWED")
    actor = User.objects.filter(
        pk=fixture_id, tenant_id=run.tenant_id, is_active=True, tenant__is_active=True
    ).first()
    if not actor:
        raise ValueError("EVALUATION_ACTOR_DISABLED")
    return RequestContext.for_user(actor, ctx.trace_id)


def verify_gold(ctx, case):
    gold_ids = {e["document_id"] for e in case.gold_evidence}
    if documents(ctx).filter(pk__in=gold_ids).count() != len(gold_ids):
        raise ValueError("GOLD_EVIDENCE_NOT_ACCESSIBLE")
    for evidence in case.gold_evidence:
        if (
            not documents(ctx)
            .filter(
                pk=evidence["document_id"],
                active_build__version_id=evidence.get("version_id"),
                active_build__version__sha256=evidence.get("sha256"),
                active_build__status="READY",
            )
            .exists()
        ):
            raise ValueError("FROZEN_CORPUS_CHANGED")


def verify_sources(ctx, sources):
    ids = {s["chunk_id"] for s in sources}
    if not User.objects.filter(
        pk=ctx.user.pk, tenant_id=ctx.tenant_id, is_active=True, tenant__is_active=True
    ).exists() or active_chunks(ctx).filter(pk__in=ids).count() != len(ids):
        raise GatewayError("PERMISSION_CHANGED")


async def collect_answer(ctx, question, prepared, guard):
    answer, usage, first = "", {"source": "unknown"}, None
    started = time.perf_counter()

    async def before_request():
        await sync_to_async(guard)()

    upstream = generate(question, prepared.sources, prepared=prepared, ctx=ctx, before_request=before_request)
    try:
        async for part in upstream:
            if part.get("text"):
                first = first if first is not None else (time.perf_counter() - started) * 1000
                answer += part["text"]
            if "usage" in part:
                usage = part["usage"]
    finally:
        await upstream.aclose()
    return answer, usage, first


def options_for(run):
    value = run.config_snapshot.get("retrieval_options", {})
    return RetrievalOptions(
        **{key: value[key] for key in RetrievalOptions.__dataclass_fields__ if key in value},
        **({"mode": run.config_snapshot.get("mode", "hybrid")} if "mode" not in value else {}),
    ).validate()


def perform_evaluation(run_id):
    with transaction.atomic():
        run = EvalRun.objects.select_for_update().select_related("actor__tenant", "dataset").get(pk=run_id)
        if run.status != "PENDING":
            return
        run.status, run.started_at, run.heartbeat_at = "RUNNING", timezone.now(), timezone.now()
        run.save(update_fields=["status", "started_at", "heartbeat_at"])
    cases = list(run.dataset.cases.all().order_by("case_key", "id"))
    split = run.config_snapshot.get("split")
    if split:
        cases = [c for c in cases if c.split == split]
    try:
        if run.dataset.split == "stratified" and split not in {"tuning", "holdout"}:
            raise ValueError("EVALUATION_SPLIT_REQUIRED")
        ctx = RequestContext.for_user(run.actor, run.trace_id)
        if not run.actor.is_active or not run.actor.tenant.is_active:
            raise ValueError("ACTOR_DISABLED")
        require_capability(ctx, "evaluation.run")
        verify_frozen(run.dataset)
        if run.config_snapshot.get("runtime") and run.config_snapshot["runtime"] != runtime_signature():
            raise ValueError("EVALUATION_CONFIG_CHANGED")
        if not cases:
            raise ValueError("EMPTY_EVALUATION_SPLIT")
        options = options_for(run)
        for case in cases:
            if not EvalRun.objects.filter(pk=run.pk, status="RUNNING").update(heartbeat_at=timezone.now()):
                return
            started, trace, hits, answer, metrics, error = time.perf_counter(), {}, [], "", {}, ""
            try:
                case_ctx = case_context(run, case)
                trace["trace_id"] = case_ctx.trace_id
                verify_frozen(run.dataset)
                verify_gold(case_ctx, case)
                hits = search(
                    case_ctx,
                    case.question,
                    kb_ids=run.dataset.manifest.get("kb_ids"),
                    top_k=5,
                    options=options,
                    history=case.spec.get("history", ()),
                    diagnostics=trace.setdefault("retrieval", {}),
                )
                metrics = retrieval_metrics(hits, case.gold_evidence)
                if run.config_snapshot.get("answers"):
                    # Fixture history supplies question context only; it is not gold answer text.
                    prepared = prepare_input(case.question, hits)
                    tick = time.perf_counter()

                    def guard():
                        # Re-check run initiator as well as the less privileged case actor.
                        if not User.objects.filter(
                            pk=run.actor_id, tenant_id=run.tenant_id, is_active=True, tenant__is_active=True
                        ).exists():
                            raise GatewayError("PERMISSION_CHANGED")
                        current_subject = case_context(run, case)
                        verify_gold(current_subject, case)
                        verify_sources(current_subject, prepared.sources)

                    guard()
                    answer, usage, first = async_to_sync(collect_answer)(
                        case_ctx, case.question, prepared, guard
                    )
                    guard()
                    trace["generation"] = {
                        "usage": usage,
                        "ttft_ms": first,
                        "total_ms": (time.perf_counter() - tick) * 1000,
                        "e2e_ttft_ms": (tick - started) * 1000 + first if first is not None else None,
                        "sources": [s["chunk_id"] for s in prepared.sources],
                    }
                    metrics.update(citation_metrics(answer, prepared.sources))
                verify_frozen(run.dataset)
                verify_sources(case_ctx, hits)
            except Exception as exc:
                allowed = {
                    "GOLD_EVIDENCE_NOT_ACCESSIBLE",
                    "FROZEN_CORPUS_CHANGED",
                    "EMBEDDING_PROFILE_CHANGED",
                    "FROZEN_DATASET_CHANGED",
                    "EVALUATION_ACTOR_DISABLED",
                    "EVALUATION_ACTOR_NOT_ALLOWED",
                }
                error = (
                    "QUOTA_EXCEEDED"
                    if isinstance(exc, QuotaExceeded)
                    else exc.code
                    if isinstance(exc, GatewayError)
                    else str(exc)
                    if str(exc) in allowed
                    else "CASE_FAILED"
                )
                hits, answer = [], ""
                trace = {
                    "trace_id": trace.get("trace_id", ""),
                    "failed_stages_ms": trace.get("retrieval", {}).get("stages_ms", {}),
                }
            trace["stages_ms"] = {"total": (time.perf_counter() - started) * 1000}
            with transaction.atomic():
                current = EvalRun.objects.select_for_update().get(pk=run.pk)
                if current.status != "RUNNING":
                    return
                EvalResult.objects.create(
                    tenant_id=run.tenant_id,
                    run=run,
                    case=case,
                    retrieved_ids=[h["chunk_id"] for h in hits],
                    retrieval_snapshot=hits,
                    answer=answer,
                    metrics=metrics,
                    error=error,
                    trace=trace,
                )
        metrics = aggregate(list(run.results.all()), cases, provider=settings.EMBEDDING_PROVIDER)
        status = "COMPLETED" if metrics["errors"] == 0 else "COMPLETED_WITH_ERRORS"
    except Exception as exc:
        allowed = {
            "ACTOR_DISABLED",
            "FROZEN_CORPUS_CHANGED",
            "EMBEDDING_PROFILE_CHANGED",
            "FROZEN_DATASET_CHANGED",
            "EVALUATION_CONFIG_CHANGED",
            "EMPTY_EVALUATION_SPLIT",
            "EVALUATION_SPLIT_REQUIRED",
        }
        status, metrics = (
            "FAILED",
            {
                "error": str(exc) if str(exc) in allowed else "EVALUATION_FAILED",
                "case_count": len(cases),
                "errors": len(cases),
            },
        )
    EvalRun.objects.filter(pk=run.pk, status="RUNNING").update(
        status=status, metrics=metrics, finished_at=timezone.now(), heartbeat_at=timezone.now()
    )


def dispatch_evaluation(run_id):
    import logging

    from .tasks import evaluate

    try:
        evaluate.delay(str(run_id))
    except Exception:
        logging.getLogger(__name__).warning("evaluation_dispatch_failed run_id=%s", run_id)
