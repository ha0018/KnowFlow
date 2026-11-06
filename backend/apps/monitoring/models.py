from django.db import models

from common.models import AuditEvent, TenantEntity

__all__ = ["AuditEvent", "ModelCallLog", "UsageReservation"]


class UsageReservation(TenantEntity):
    user = models.ForeignKey("users.User", on_delete=models.PROTECT)
    run = models.ForeignKey("conversations.Run", null=True, blank=True, on_delete=models.PROTECT)
    task = models.ForeignKey("documents.DocumentTask", null=True, blank=True, on_delete=models.PROTECT)
    period = models.DateField()
    task_type = models.CharField(max_length=30)
    model = models.CharField(max_length=120)
    trace_id = models.CharField(max_length=64)
    status = models.CharField(
        max_length=12,
        default="reserved",
        choices=[("reserved", "Reserved"), ("settled", "Settled"), ("released", "Released")],
    )
    reserved_tokens = models.PositiveBigIntegerField()
    charged_tokens = models.PositiveBigIntegerField(default=0)
    dispatched = models.BooleanField(default=False)
    usage = models.JSONField(default=dict)
    price_snapshot = models.JSONField(default=dict)
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)

    class Meta(TenantEntity.Meta):
        indexes = [
            models.Index(fields=["tenant", "period", "user"]),
            models.Index(fields=["status", "expires_at"]),
        ]


class ModelCallLog(TenantEntity):
    reservation = models.OneToOneField(UsageReservation, null=True, blank=True, on_delete=models.PROTECT)
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
