from django.db import transaction
from django.db.models import F
from rest_framework.exceptions import ValidationError

from apps.users.models import Role, Tenant, User
from common.context import audit


@transaction.atomic
def replace_acl(ctx, obj, entries):
    # Serialize all authorization mutations with generation/publication checks.
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    users = {str(u) for u in User.objects.filter(tenant_id=ctx.tenant_id).values_list("id", flat=True)}
    roles = {str(r) for r in Role.objects.filter(tenant_id=ctx.tenant_id).values_list("id", flat=True)}
    seen = set()
    for entry in entries:
        uid, rid = entry.get("user_id"), entry.get("role_id")
        if bool(uid) == bool(rid) or (uid and str(uid) not in users) or (rid and str(rid) not in roles):
            raise ValidationError("授权主体必须是本租户的一名用户或一个角色")
        key = (str(uid), str(rid))
        if key in seen:
            raise ValidationError("授权主体重复")
        seen.add(key)
    obj.acl.all().delete()
    for entry in entries:
        obj.acl.create(tenant_id=ctx.tenant_id, **entry)
    Tenant.objects.filter(pk=ctx.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
    audit(ctx, "acl.replace", obj)


def bump_epoch(ctx):
    Tenant.objects.filter(pk=ctx.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
