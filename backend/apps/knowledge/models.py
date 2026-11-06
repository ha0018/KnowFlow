from django.conf import settings
from django.db import models
from django.db.models import Q

from common.models import TenantEntity


class KnowledgeBase(TenantEntity):
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, max_length=2000)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    visibility = models.CharField(
        max_length=10, choices=[("private", "私有"), ("tenant", "租户内")], default="private"
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class ACLBase(TenantEntity):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    role = models.ForeignKey("users.Role", null=True, blank=True, on_delete=models.CASCADE)
    access_level = models.CharField(
        max_length=10, choices=[("read", "读取"), ("edit", "编辑"), ("manage", "管理")]
    )

    class Meta:
        abstract = True


class KnowledgeBaseACL(ACLBase):
    knowledge_base = models.ForeignKey(KnowledgeBase, related_name="acl", on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False, role__isnull=True) | Q(user__isnull=True, role__isnull=False)
                ),
                name="kb_acl_one_subject",
            ),
            models.UniqueConstraint(fields=["knowledge_base", "user"], name="kb_acl_user"),
            models.UniqueConstraint(fields=["knowledge_base", "role"], name="kb_acl_role"),
        ]
