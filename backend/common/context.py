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
