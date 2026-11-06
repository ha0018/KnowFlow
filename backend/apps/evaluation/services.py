from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ai_engine.retrieval import search
from apps.documents.selectors import documents
from apps.users.permissions import require_capability
from common.context import RequestContext

from .models import EvalResult, EvalRun


def perform_evaluation(run_id):
    with transaction.atomic():
        run = EvalRun.objects.select_for_update().select_related("actor__tenant", "dataset").get(pk=run_id)
        if run.status != "PENDING":
            return
        run.status, run.started_at = "RUNNING", timezone.now()
        run.save()
    ctx = RequestContext.for_user(run.actor, run.trace_id)
    recalls, reciprocal, errors = [], [], 0
    try:
        if not run.actor.is_active or not run.actor.tenant.is_active:
            raise ValueError("ACTOR_DISABLED")
        require_capability(ctx, "evaluation.run")
        for case in run.dataset.cases.all():
            try:
                gold_ids = [item["document_id"] for item in case.gold_evidence]
                if documents(ctx).filter(pk__in=gold_ids).count() != len(set(gold_ids)):
                    raise ValueError("GOLD_EVIDENCE_NOT_ACCESSIBLE")
                for evidence in case.gold_evidence:
                    if (
                        not documents(ctx)
                        .filter(
                            pk=evidence["document_id"],
                            active_build__version_id=evidence.get("version_id"),
                            active_build__version__sha256=evidence.get("sha256"),
                        )
                        .exists()
                    ):
                        raise ValueError("FROZEN_CORPUS_CHANGED")
                hits = search(ctx, case.question, top_k=5, mode=run.config_snapshot["mode"])
                ids = list(dict.fromkeys(hit["document_id"] for hit in hits))
                ranks = [ids.index(doc) + 1 for doc in gold_ids if doc in ids]
                metric = {
                    "doc_recall_at_5": len(set(ids) & set(gold_ids)) / len(set(gold_ids))
                    if gold_ids
                    else None,
                    "mrr_at_5": 1 / min(ranks) if ranks else (0.0 if gold_ids else None),
                }
                if gold_ids:
                    recalls.append(metric["doc_recall_at_5"])
                    reciprocal.append(metric["mrr_at_5"])
                EvalResult.objects.create(
                    tenant_id=ctx.tenant_id,
                    run=run,
                    case=case,
                    retrieved_ids=[h["chunk_id"] for h in hits],
                    metrics=metric,
                )
            except Exception:
                errors += 1
                EvalResult.objects.create(tenant_id=ctx.tenant_id, run=run, case=case, error="CASE_FAILED")
        run.metrics = {
            "doc_recall_at_5": sum(recalls) / len(recalls) if recalls else None,
            "mrr_at_5": sum(reciprocal) / len(reciprocal) if reciprocal else None,
            "answerable_evaluated": len(recalls),
            "case_count": run.dataset.cases.count(),
            "errors": errors,
            "provider": settings.EMBEDDING_PROVIDER,
            "note": "小型开发集召回实验；不包含回答质量、证据支撑率或生产性能验收。",
        }
        run.status = "COMPLETED" if errors == 0 else "COMPLETED_WITH_ERRORS"
    except Exception:
        run.status = "FAILED"
        run.metrics = {"error": "EVALUATION_FAILED"}
    run.finished_at = timezone.now()
    run.save()


def dispatch_evaluation(run_id):
    import logging

    from .tasks import evaluate

    try:
        evaluate.delay(str(run_id))
    except Exception:
        logging.getLogger(__name__).warning("evaluation_dispatch_failed run_id=%s", run_id)
