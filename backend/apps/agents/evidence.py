import json

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from ai_engine.gateway.config import GatewayError
from apps.conversations.models import MessageReference
from apps.documents.selectors import active_chunks
from apps.users.models import Tenant, User
from apps.users.permissions import require_capability
from common.context import RequestContext

from .models import ToolCall
from .policy import fresh_context


def wire_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def shorten(text, size):
    return text.encode()[: max(0, size)].decode(errors="ignore")


@transaction.atomic
def observe(ctx, run, call, result, available):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    ctx = fresh_context(ctx, run)
    call = ToolCall.objects.select_for_update().get(pk=call.pk, run=run, actor=ctx.user, status="completed")
    refs = list(
        run.output_message.references.select_related("chunk__document", "chunk__build__version").all()
    )
    existing = {str(ref.chunk_id): ref for ref in refs}
    data_calls = list(ToolCall.objects.filter(run=run, status="completed").exclude(source_id=""))
    rank = len(refs) + len(data_calls)
    if call.tool_name == "DataAnalysisTool":
        data = {**result, "captured_at": timezone.now().isoformat()}
        observation = {"analysis": {"source_id": f"S{rank + 1}", "data": data}}
        while data.get("groups") and wire_size(observation) > available:
            data["groups"].pop()
            data["groups_truncated"] = True
        if wire_size(observation) > available:
            raise GatewayError("AGENT_EVIDENCE_BUDGET_EXCEEDED")
        call.source_id = f"S{rank + 1}"
        call.result_summary = {**call.result_summary, "analysis": data}
        call.save(update_fields=["source_id", "result_summary"])
        return observation
    observation = {
        "sources": [],
        "summary": call.tool_name == "DocumentSummaryTool",
        "truncated": bool(result.get("truncated")),
    }
    for source in result.get("sources", []):
        ref = existing.get(source["chunk_id"])
        if ref:
            item = {
                "source_id": ref.source_id,
                "document_id": str(ref.chunk.document_id),
                "title": ref.chunk.document.title,
                "version": ref.chunk.build.version.version_no,
                "content": ref.excerpt_snapshot,
            }
            if wire_size({**observation, "sources": [*observation["sources"], item]}) <= available:
                observation["sources"].append(item)
            else:
                observation["truncated"] = True
            continue
        chunk = (
            active_chunks(ctx)
            .select_related("document", "build__version")
            .filter(pk=source["chunk_id"])
            .first()
        )
        if not chunk:
            raise GatewayError("PERMISSION_CHANGED")
        item = {
            "source_id": f"S{rank + 1}",
            "document_id": str(chunk.document_id),
            "title": chunk.document.title,
            "version": chunk.build.version.version_no,
            "content": "",
        }
        overhead = wire_size({**observation, "sources": [*observation["sources"], item]})
        excerpt = shorten(source.get("content", source.get("text", "")), min(1200, available - overhead - 64))
        if not excerpt:
            observation["truncated"] = True
            continue
        if not chunk.content.startswith(excerpt):
            raise GatewayError("TOOL_SOURCE_INVALID")
        item["content"] = excerpt
        if wire_size({**observation, "sources": [*observation["sources"], item]}) > available:
            observation["truncated"] = True
            continue
        locator = {**chunk.locator, "char_end": chunk.locator.get("char_start", 0) + len(excerpt)}
        ref = MessageReference.objects.create(
            tenant_id=ctx.tenant_id,
            message_id=run.output_message_id,
            chunk=chunk,
            source_id=item["source_id"],
            rank=rank + 1,
            locator_snapshot=locator,
            excerpt_snapshot=excerpt,
            retrieval_scores={"rrf": source.get("score", 0), "tool_call_id": str(call.pk)},
        )
        rank += 1
        existing[source["chunk_id"]] = ref
        observation["sources"].append(item)
        observation["truncated"] |= len(excerpt) < len(source.get("content", source.get("text", "")))
    return observation


def analysis_visible(ctx, call):
    user = (
        User.objects.select_related("tenant")
        .filter(pk=ctx.user.pk, tenant_id=ctx.tenant_id, is_active=True, tenant__is_active=True)
        .first()
    )
    if not user or user.pk != call.actor_id or user.tenant.acl_epoch != call.guard_snapshot.get("acl_epoch"):
        return False
    try:
        require_capability(RequestContext.for_user(user, ctx.trace_id), "agent.execute")
        return True
    except PermissionDenied:
        return False


def analysis_references(ctx, run):
    return [
        {
            "id": str(c.pk),
            "source_id": c.source_id,
            "kind": "analysis",
            "title": "演示业务统计",
            "version": None,
            "locator": {"captured_at": c.result_summary.get("analysis", {}).get("captured_at")},
        }
        for c in run.toolcall_set.filter(status="completed").exclude(source_id="")
        if analysis_visible(ctx, c)
    ]


def analysis_reference(ctx, call_id):
    call = (
        ToolCall.objects.filter(
            pk=call_id, tenant_id=ctx.tenant_id, actor=ctx.user, status="completed", run__isnull=False
        )
        .exclude(source_id="")
        .first()
    )
    if not call or not analysis_visible(ctx, call):
        raise Http404()
    data = call.result_summary.get("analysis", {})
    return {
        "id": str(call.pk),
        "kind": "analysis",
        "source_id": call.source_id,
        "title": "演示业务统计",
        "version": None,
        "locator": {
            "captured_at": data.get("captured_at"),
            "group_by": data.get("group_by"),
            "date_from": data.get("date_from"),
            "date_to": data.get("date_to"),
        },
        "excerpt": json.dumps(data, ensure_ascii=False, indent=2),
    }
