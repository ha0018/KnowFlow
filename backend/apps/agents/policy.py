from django.conf import settings

from ai_engine.gateway.config import GatewayError
from apps.conversations.models import Run
from apps.documents.selectors import active_chunks
from apps.users.models import User
from apps.users.permissions import require_capability
from common.context import RequestContext


def limits():
    values = {
        "max_tools": settings.AGENT_MAX_TOOL_CALLS,
        "tool_seconds": settings.AGENT_TOOL_TIMEOUT_SECONDS,
        "total_seconds": settings.AGENT_TOTAL_TIMEOUT_SECONDS,
        "token_budget": settings.AGENT_TOKEN_BUDGET,
        "evidence_bytes": settings.AGENT_EVIDENCE_BYTES,
        "output_bytes": settings.AGENT_OUTPUT_BYTES,
    }
    if (
        type(values["max_tools"]) is not int
        or not 1 <= values["max_tools"] <= 4
        or not 0.1 <= values["tool_seconds"] <= 10
        or not 0.2 <= values["total_seconds"] <= 60
        or not 1024 <= values["token_budget"] <= 100000
        or not 512 <= values["evidence_bytes"] <= 12000
        or not 512 <= values["output_bytes"] <= 16000
    ):
        raise GatewayError("AGENT_CONFIG_INVALID")
    return values


def fresh_context(ctx, run=None, sources=()):
    user = (
        User.objects.select_related("tenant")
        .filter(pk=ctx.user.pk, tenant_id=ctx.tenant_id, is_active=True, tenant__is_active=True)
        .first()
    )
    if not user:
        raise GatewayError("PERMISSION_CHANGED")
    current = RequestContext.for_user(user, ctx.trace_id)
    require_capability(current, "agent.execute")
    epoch = run.acl_epoch if run is not None else ctx.acl_epoch
    if user.tenant.acl_epoch != epoch:
        raise GatewayError("PERMISSION_CHANGED")
    if run is not None:
        state = (
            Run.objects.filter(pk=run.pk, tenant_id=ctx.tenant_id, user_id=user.pk)
            .values_list("status", flat=True)
            .first()
        )
        if state != "streaming":
            raise GatewayError("AGENT_CANCELLED" if state == "cancelled" else "AGENT_RUN_STOPPED")
    ids = {source["chunk_id"] for source in sources if source.get("chunk_id")}
    if ids and active_chunks(current).filter(pk__in=ids).count() != len(ids):
        raise GatewayError("PERMISSION_CHANGED")
    return current
