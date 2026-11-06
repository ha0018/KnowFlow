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


class BusinessIssue(TenantEntity):
    occurred_at = models.DateTimeField()
    product = models.CharField(max_length=80)
    category = models.CharField(max_length=80)
    description = models.CharField(max_length=1000)
