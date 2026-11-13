import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    user: object
    tenant_id: uuid.UUID
    trace_id: str
    acl_epoch: int

    @classmethod
    def for_user(cls, user, trace_id=None):
        return cls(user, user.tenant_id, trace_id or uuid.uuid4().hex, user.tenant.acl_epoch)

    @classmethod
    def from_request(cls, request):
        return cls.for_user(request.user, getattr(request, "trace_id", None))


def audit(ctx, action, obj):
    from common.models import AuditEvent

    AuditEvent.objects.create(
        tenant_id=ctx.tenant_id,
        actor=ctx.user,
        action=action,
        resource_type=obj._meta.label_lower,
        resource_id=str(obj.pk),
        trace_id=ctx.trace_id,
    )


def reload_context(ctx):
    from rest_framework.exceptions import PermissionDenied

    from apps.users.models import User

    user = (
        User.objects.select_related("tenant")
        .filter(
            pk=ctx.user.pk,
            tenant_id=ctx.tenant_id,
            is_active=True,
            tenant__is_active=True,
        )
        .first()
    )
    if not user:
        raise PermissionDenied("账号或租户已停用")
    return RequestContext.for_user(user, ctx.trace_id)
