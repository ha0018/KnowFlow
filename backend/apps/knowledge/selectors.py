from django.db.models import Q
from django.shortcuts import get_object_or_404

from .models import KnowledgeBase

LEVELS = {"read": ["read", "edit", "manage"], "edit": ["edit", "manage"], "manage": ["manage"]}


def subject_q(ctx, prefix=""):
    return Q(**{f"{prefix}user_id": ctx.user.pk}) | Q(
        **{f"{prefix}role_id__in": ctx.user.roles.filter(tenant_id=ctx.tenant_id).values("id")}
    )


def knowledge_bases(ctx, level="read"):
    qs = KnowledgeBase.objects.filter(tenant_id=ctx.tenant_id, deleted_at__isnull=True)
    if ctx.user.is_tenant_admin:
        return qs
    grant = Q(owner=ctx.user) | (subject_q(ctx, "acl__") & Q(acl__access_level__in=LEVELS[level]))
    if level == "read":
        grant |= Q(visibility="tenant")
    return qs.filter(grant).distinct()


def get_knowledge_base(ctx, pk, level="read"):
    return get_object_or_404(knowledge_bases(ctx, level), pk=pk)
