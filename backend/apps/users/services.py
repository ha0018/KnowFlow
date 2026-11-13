from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied, ValidationError

from common.context import RequestContext, audit

from .models import Permission, Role, Tenant, User


def current_admin(ctx):
    user = (
        User.objects.select_related("tenant")
        .filter(
            pk=ctx.user.pk,
            tenant_id=ctx.tenant_id,
            is_active=True,
            tenant__is_active=True,
            is_tenant_admin=True,
        )
        .first()
    )
    if not user:
        raise PermissionDenied("仅当前有效的租户管理员可以管理成员和角色")
    return RequestContext.for_user(user, ctx.trace_id)


def permissions(codes):
    values = list(Permission.objects.filter(code__in=codes))
    if len(values) != len(set(codes)):
        raise ValidationError("未知操作权限")
    return values


@transaction.atomic
def update_member(ctx, member_id, values):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    ctx = current_admin(ctx)
    member = get_object_or_404(User, tenant_id=ctx.tenant_id, pk=member_id)
    if values.get("is_active") is False and member.pk == ctx.user.pk:
        raise ValidationError("不能停用当前登录账号")
    if (
        values.get("is_active") is False
        and member.is_tenant_admin
        and not User.objects.filter(
            tenant_id=ctx.tenant_id,
            is_tenant_admin=True,
            is_active=True,
        )
        .exclude(pk=member.pk)
        .exists()
    ):
        raise ValidationError("必须保留至少一名有效管理员")
    for name, value in values.items():
        setattr(member, name, value)
    member.save(update_fields=list(values))
    Tenant.objects.filter(pk=ctx.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
    audit(ctx, "member.update", member)
    return member


@transaction.atomic
def update_role(ctx, role_id, values):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    ctx = current_admin(ctx)
    role = get_object_or_404(Role, tenant_id=ctx.tenant_id, pk=role_id)
    if "permission_codes" in values:
        role.permissions.set(permissions(values["permission_codes"]))
    if "name" in values:
        role.name = values["name"]
        role.save(update_fields=["name", "updated_at"])
    Tenant.objects.filter(pk=ctx.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
    audit(ctx, "role.update", role)
    return role
