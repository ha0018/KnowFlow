from django.db import models

from common.models import AuditEvent, TenantEntity

__all__ = ["AuditEvent", "ModelCallLog"]


class ModelCallLog(TenantEntity):
    run = models.ForeignKey("conversations.Run", null=True, blank=True, on_delete=models.SET_NULL)
    task = models.ForeignKey("documents.DocumentTask", null=True, blank=True, on_delete=models.SET_NULL)
    trace_id = models.CharField(max_length=64)
    task_type = models.CharField(max_length=30)
    model = models.CharField(max_length=120)
    status = models.CharField(max_length=30)
    usage = models.JSONField(default=dict)
    timing = models.JSONField(default=dict)
    price_snapshot = models.JSONField(default=dict)

    class Meta(TenantEntity.Meta):
        indexes = [models.Index(fields=["tenant", "created_at"])]
