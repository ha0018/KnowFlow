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

from apps.documents.selectors import active_chunks, chunk_readable
from apps.knowledge.selectors import knowledge_bases
from apps.users.models import Tenant, User

from .models import Conversation, Message, MessageReference, Run

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class Conflict(APIException):
    status_code = 409
    default_detail = "请求已存在或另一个运行尚未结束"


def get_conversation(ctx, pk):
    return get_object_or_404(Conversation, pk=pk, tenant_id=ctx.tenant_id, user=ctx.user)


@transaction.atomic
def begin_run(ctx, conversation_id, question, key, parent_id=None):
    # Lock user for both per-user idempotency and concurrency budget across conversations.
    User.objects.select_for_update().get(pk=ctx.user.pk)
    conversation = get_conversation(ctx, conversation_id)
    Conversation.objects.select_for_update().get(pk=conversation.pk)
    digest = hashlib.sha256(json.dumps([str(conversation_id), question, str(parent_id)]).encode()).hexdigest()
    prior = Run.objects.filter(tenant_id=ctx.tenant_id, user=ctx.user, idempotency_key=key).first()
    if prior:
        if prior.request_hash != digest:
            raise Conflict("相同幂等键不能用于不同请求")
        return prior, False
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
        config_snapshot={
            "provider": settings.AI_PROVIDER,
            "model": settings.AI_MODEL,
            "embedding_profile": settings.EMBEDDING_PROFILE,
            "prompt": "grounded-v1",
            "retrieval": "rrf-v1",
            "kb_ids": [str(pk) for pk in scope_ids],
        },
    )
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
        content = re.sub(r"\[(S\d+)\]", lambda m: m[0] if m[1] in valid else "[来源无效]", content)
    Message.objects.filter(pk=run.output_message_id).update(content=content, status=status or "streaming")
    run.heartbeat_at = timezone.now()
    if status:
        run.status, run.error_code, run.usage = status, error, usage or {"source": "unknown"}
        from apps.monitoring.models import ModelCallLog

        ModelCallLog.objects.create(
            tenant_id=run.tenant_id,
            run=run,
            trace_id=run.trace_id,
            task_type="generation",
            model=run.config_snapshot.get("model") or "demo-extractive",
            status=status,
            usage=run.usage,
        )
    run.save()
    return run.status


def message_visible(ctx, message):
    return all(chunk_readable(ctx, ref.chunk_id) for ref in message.references.all())


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
        result.append(
            {
                "id": str(message.pk),
                "role": message.role,
                "seq": message.seq,
                "content": message.content if visible else "此回复包含已撤权资料，内容已隐藏。",
                "status": message.status if visible else "redacted",
                "references": [reference_data(r) for r in message.references.all()] if visible else [],
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
        from apps.monitoring.models import ModelCallLog

        ModelCallLog.objects.create(
            tenant_id=run.tenant_id,
            run=run,
            trace_id=run.trace_id,
            task_type="generation",
            model=run.config_snapshot.get("model") or "demo-extractive",
            status="cancelled",
            usage={"source": "unknown"},
        )
    return run


def interrupt_stale_runs():
    for run in Run.objects.filter(status="streaming", heartbeat_at__lt=timezone.now() - timedelta(minutes=2)):
        save_progress(run.pk, run.output_message.content, "interrupted", error="HEARTBEAT_EXPIRED")
