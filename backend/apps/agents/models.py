from django.db import models

from common.models import TenantEntity


class ToolCall(TenantEntity):
    actor = models.ForeignKey("users.User", on_delete=models.PROTECT)
    run = models.ForeignKey("conversations.Run", null=True, blank=True, on_delete=models.SET_NULL)
    tool_name = models.CharField(max_length=80)
    schema_version = models.CharField(max_length=30, default="v1")
    args_redacted = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="running")
    latency_ms = models.PositiveIntegerField(default=0)
    result_summary = models.JSONField(default=dict)
    trace_id = models.CharField(max_length=64)
    step_index = models.PositiveSmallIntegerField(default=0)
    arguments_hash = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    finished_at = models.DateTimeField(null=True)
    guard_snapshot = models.JSONField(default=dict)
    source_id = models.CharField(max_length=12, blank=True)

    class Meta(TenantEntity.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["run", "step_index"],
                condition=models.Q(run__isnull=False, step_index__gt=0),
                name="unique_agent_step",
            )
        ]


class BusinessIssue(TenantEntity):
    occurred_at = models.DateTimeField()
    product = models.CharField(max_length=80)
    category = models.CharField(max_length=80)
    description = models.CharField(max_length=1000)
