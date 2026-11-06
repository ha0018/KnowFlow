import uuid

from django.db import models


class Entity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at", "id"]


class TenantEntity(Entity):
    tenant = models.ForeignKey("users.Tenant", on_delete=models.PROTECT)

    class Meta(Entity.Meta):
        abstract = True


class AuditEvent(TenantEntity):
    actor = models.ForeignKey("users.User", null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=80)
    resource_type = models.CharField(max_length=80)
    resource_id = models.CharField(max_length=80)
    trace_id = models.CharField(max_length=64)
    outcome = models.CharField(max_length=20, default="success")

    class Meta(TenantEntity.Meta):
        app_label = "monitoring"
