import hashlib
import json
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from ai_engine.gateway.budget import prepare_input
from ai_engine.gateway.config import profile
from ai_engine.retrieval.options import defaults
from apps.documents.selectors import active_chunks, chunk_readable
from apps.knowledge.selectors import knowledge_bases
from apps.users.models import Tenant, User
from common.context import RequestContext

from .models import Conversation, Message, MessageReference, Run

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class Conflict(APIException):
    status_code = 409
    default_detail = "请求已存在或另一个运行尚未结束"


def get_conversation(ctx, pk):
    return get_object_or_404(Conversation, pk=pk, tenant_id=ctx.tenant_id, user=ctx.user)


@transaction.atomic
def begin_run(ctx, conversation_id, question, key, parent_id=None, *, kind="rag"):
    # All admission/accounting paths lock tenant first to avoid lock inversion.
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    User.objects.select_for_update().get(pk=ctx.user.pk)
    conversation = get_conversation(ctx, conversation_id)
    Conversation.objects.select_for_update().get(pk=conversation.pk)
    if kind not in {"rag", "agent"}:
        raise ValidationError("未知运行类型")
    if kind == "agent":
        from apps.agents.policy import fresh_context

        ctx = fresh_context(ctx)
    identity = [str(conversation_id), question, str(parent_id)]
    if kind == "agent":
        identity.append(kind)
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    prior = Run.objects.filter(tenant_id=ctx.tenant_id, user=ctx.user, idempotency_key=key).first()
    if prior:
        if prior.request_hash != digest:
            raise Conflict("相同幂等键不能用于不同请求")
        return prior, False
    model = profile("generation")
    if kind == "rag":
        profile("embedding")
    prepare_input(question, [])
    if Run.objects.filter(user=ctx.user, status="streaming").count() >= settings.MAX_CONCURRENT_RUNS:
        raise Conflict("并发运行数已达上限")
    if Run.objects.filter(conversation=conversation, status="streaming").exists():
        raise Conflict()
    scope_ids = list(conversation.scope.values_list("knowledge_base_id", flat=True))
    if scope_ids and knowledge_bases(ctx).filter(pk__in=scope_ids).count() != len(scope_ids):
        raise ValidationError("对话范围包含已撤权知识库，请创建新对话")
    parent = (
        get_object_or_404(Message, pk=parent_id, conversation=conversation, role="assistant")
        if parent_id
        else None
    )
    n = conversation.messages.aggregate(n=Max("seq"))["n"] or 0
    incoming = Message.objects.create(
        tenant_id=ctx.tenant_id, conversation=conversation, seq=n + 1, role="user", content=question
    )
    outgoing = Message.objects.create(
        tenant_id=ctx.tenant_id,
        conversation=conversation,
        seq=n + 2,
        role="assistant",
        status="streaming",
        parent=parent,
    )
    run = Run.objects.create(
        tenant_id=ctx.tenant_id,
        conversation=conversation,
        user=ctx.user,
        input_message=incoming,
        output_message=outgoing,
        idempotency_key=key,
        request_hash=digest,
        trace_id=ctx.trace_id,
        acl_epoch=Tenant.objects.get(pk=ctx.tenant_id).acl_epoch,
        kind=kind,
        config_snapshot={
            "provider": model.provider,
            "model": model.model,
            "embedding_profile": settings.EMBEDDING_PROFILE,
            "prompt": "grounded-v2",
            "retrieval": "rrf-v1",
            "retrieval_options": defaults().snapshot(),
            "kb_ids": [str(pk) for pk in scope_ids],
        },
    )
    if kind == "agent":
        from apps.agents.policy import limits

        run.config_snapshot.update(agent=limits(), prompt="agent-tools-v1", tool_schema="agent-tools-v2")
        run.save(update_fields=["config_snapshot"])
    if conversation.title == "新对话":
        conversation.title = question[:80]
        conversation.save(update_fields=["title"])
    return run, True


def run_is_allowed(ctx, run_id):
    run = Run.objects.get(pk=run_id, tenant_id=ctx.tenant_id, user=ctx.user)
    return (
        run.status == "streaming"
        and User.objects.filter(pk=ctx.user.pk, is_active=True, tenant__is_active=True).exists()
        and Tenant.objects.filter(pk=ctx.tenant_id, acl_epoch=run.acl_epoch).exists()
    )


@transaction.atomic
def attach_sources(ctx, run, sources):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    if not run_is_allowed(ctx, run.pk):
        raise ValidationError("PERMISSION_CHANGED")
    for rank, source in enumerate(sources, 1):
        if not active_chunks(ctx).filter(pk=source["chunk_id"]).exists():
            raise ValidationError("PERMISSION_CHANGED")
        MessageReference.objects.create(
            tenant_id=ctx.tenant_id,
            message_id=run.output_message_id,
            chunk_id=source["chunk_id"],
            source_id=source["source_id"],
            rank=rank,
            locator_snapshot=source["locator"],
            excerpt_snapshot=source["content"],
            retrieval_scores={"rrf": source["score"]},
        )


@transaction.atomic
def save_progress(run_id, content, status=None, usage=None, error=""):
    run = Run.objects.select_for_update().get(pk=run_id)
    if run.status in TERMINAL:
        if run.status == "cancelled" and status:
            Message.objects.filter(pk=run.output_message_id).update(content=content, status="cancelled")
        return run.status
    if status:
        # Unknown source IDs cannot become citations. Preserve valid source markers only.
        valid = set(run.output_message.references.values_list("source_id", flat=True))
        if run.kind == "agent":
            valid.update(
                run.toolcall_set.filter(status="completed")
                .exclude(source_id="")
                .values_list("source_id", flat=True)
            )
        content = re.sub(r"\[(S\d+)\]", lambda m: m[0] if m[1] in valid else "[来源无效]", content)
    Message.objects.filter(pk=run.output_message_id).update(content=content, status=status or "streaming")
    run.heartbeat_at = timezone.now()
    if status:
        run.status, run.error_code, run.usage = status, error, usage or {"source": "unknown"}
    run.save()
    return run.status


def message_visible(ctx, message):
    run = getattr(message, "output_run", None)
    if run is not None and run.kind == "agent":
        from rest_framework.exceptions import PermissionDenied

        from apps.agents.evidence import analysis_visible
        from apps.users.permissions import require_capability

        actor = (
            User.objects.select_related("tenant")
            .filter(pk=ctx.user.pk, is_active=True, tenant__is_active=True)
            .first()
        )
        if not actor:
            return False
        try:
            require_capability(RequestContext.for_user(actor, ctx.trace_id), "agent.execute")
        except PermissionDenied:
            return False
        if any(
            not analysis_visible(ctx, call)
            for call in run.toolcall_set.filter(status="completed").exclude(source_id="")
        ):
            return False
    return all(chunk_readable(ctx, ref.chunk_id) for ref in message.references.all())


def model_history(ctx, run):
    pairs = []
    messages = list(
        Message.objects.filter(conversation_id=run.conversation_id, seq__lt=run.input_message.seq)
        .order_by("-seq")
        .prefetch_related("references")[:40]
    )
    by_seq = {m.seq: m for m in messages}
    for answer in reversed(messages):
        if answer.role != "assistant" or answer.status != "completed":
            continue
        if not message_visible(ctx, answer):
            continue
        question = by_seq.get(answer.seq - 1)
        refs = list(answer.references.all())
        if not question or question.role != "user" or not refs:
            continue
        # Current permissions AND current published versions govern replay.
        ids = {ref.chunk_id for ref in refs}
        if active_chunks(ctx).filter(pk__in=ids).count() != len(ids):
            continue
        text = re.sub(r"\[S\d+\]", "", answer.content)
        pairs.append(
            [
                {"role": "user", "content": question.content},
                {"role": "assistant", "content": "历史回答（不是本轮证据）：" + text},
            ]
        )
    return pairs


@transaction.atomic
def prepare_generation(ctx, run, sources):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    if not run_is_allowed(ctx, run.pk):
        from ai_engine.gateway.config import GatewayError

        raise GatewayError("PERMISSION_CHANGED")
    prepared = prepare_input(run.input_message.content, sources, model_history(ctx, run))
    snapshot = dict(run.config_snapshot)
    snapshot.update(
        input_tokens_estimate=prepared.input_tokens_estimate,
        history_messages=prepared.history_messages,
        token_estimator="utf8-upper-bound-v1",
        max_output_tokens=settings.GENERATION_MAX_OUTPUT_TOKENS,
    )
    Run.objects.filter(pk=run.pk).update(config_snapshot=snapshot)
    return prepared


def reference_data(ref):
    return {
        "id": str(ref.pk),
        "source_id": ref.source_id,
        "title": ref.chunk.document.title,
        "version": ref.chunk.build.version.version_no,
        "locator": ref.locator_snapshot,
    }


def history(ctx, conversation):
    result = []
    for message in conversation.messages.prefetch_related(
        "references__chunk__document", "references__chunk__build__version"
    ):
        visible = message_visible(ctx, message)
        references = [reference_data(r) for r in message.references.all()] if visible else []
        run = getattr(message, "output_run", None)
        if visible and run is not None and run.kind == "agent":
            from apps.agents.evidence import analysis_references

            references.extend(analysis_references(ctx, run))
            references.sort(key=lambda r: int(r["source_id"][1:]))
        result.append(
            {
                "id": str(message.pk),
                "role": message.role,
                "seq": message.seq,
                "content": message.content if visible else "此回复包含已撤权资料，内容已隐藏。",
                "status": message.status if visible else "redacted",
                "references": references,
            }
        )
    return result


@transaction.atomic
def cancel_run(ctx, run_id):
    run = get_object_or_404(
        Run.objects.select_for_update(), pk=run_id, tenant_id=ctx.tenant_id, user=ctx.user
    )
    if run.status not in TERMINAL:
        run.status = "cancelled"
        run.save(update_fields=["status"])
        Message.objects.filter(pk=run.output_message_id).update(status="cancelled")
    return run


def interrupt_stale_runs():
    # Leave one 30-second beat interval inside the two-minute recovery window.
    cutoff = timezone.now() - timedelta(seconds=90)
    for run_id in Run.objects.filter(status="streaming", heartbeat_at__lt=cutoff).values_list(
        "id", flat=True
    ):
        with transaction.atomic():
            run = Run.objects.select_for_update().get(pk=run_id)
            # A heartbeat or terminal transition may have happened since the scan.
            if run.status == "streaming" and run.heartbeat_at < cutoff:
                save_progress(run.pk, run.output_message.content, "interrupted", error="HEARTBEAT_EXPIRED")
