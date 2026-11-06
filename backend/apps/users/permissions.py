from rest_framework.exceptions import PermissionDenied


def require_capability(ctx, code):
    if (
        not ctx.user.is_tenant_admin
        and not ctx.user.roles.filter(tenant_id=ctx.tenant_id, permissions__code=code).exists()
    ):
        raise PermissionDenied("没有执行此操作的权限")
