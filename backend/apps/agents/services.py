import time
from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncMonth
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from ai_engine.retrieval import search
from apps.documents.selectors import active_chunks, get_document
from apps.knowledge.selectors import knowledge_bases
from apps.users.models import Tenant
from apps.users.permissions import require_capability
from common.context import audit

from .models import BusinessIssue, ToolCall


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise ValidationError({"non_field_errors": ["参数包含未知字段或格式错误"]})
        return super().to_internal_value(data)


class SearchArgs(StrictSerializer):
    query = serializers.CharField(max_length=2000)
    kb_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    top_k = serializers.IntegerField(min_value=1, max_value=8, default=5)


class SummaryArgs(StrictSerializer):
    document_id = serializers.UUIDField()
    focus = serializers.CharField(max_length=500, required=False, default="")


class AnalysisArgs(StrictSerializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    product = serializers.CharField(max_length=80, required=False)
    group_by = serializers.ChoiceField(choices=["month", "product", "category"])

    def validate(self, attrs):
        if not timedelta(0) <= attrs["date_to"] - attrs["date_from"] <= timedelta(days=366):
            raise ValidationError("时间跨度必须为 0–366 天")
        return attrs


SCHEMAS = {
    "KnowledgeSearchTool": SearchArgs,
    "DocumentSummaryTool": SummaryArgs,
    "DataAnalysisTool": AnalysisArgs,
}


def execute(ctx, name, args):
    require_capability(ctx, "agent.execute")
    if name not in SCHEMAS:
        raise ValidationError("未知工具")
    call = ToolCall.objects.create(
        tenant_id=ctx.tenant_id, actor=ctx.user, tool_name=name, trace_id=ctx.trace_id
    )  # Never log question/full document text.
    started = time.monotonic()
    try:
        serializer = SCHEMAS[name](data=args)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        if not Tenant.objects.filter(pk=ctx.tenant_id, acl_epoch=ctx.acl_epoch, is_active=True).exists():
            raise ValidationError("权限已变更")
        if name == "KnowledgeSearchTool":
            ids = params.get("kb_ids")
            if ids is not None and knowledge_bases(ctx).filter(pk__in=ids).count() != len(set(ids)):
                raise ValidationError("知识库不可用")
            result = {"sources": search(ctx, params["query"], ids, params["top_k"])}
        elif name == "DocumentSummaryTool":
            doc = get_document(ctx, params["document_id"])
            chunks = list(active_chunks(ctx).filter(document=doc).order_by("ordinal")[:8])
            # Extractive summary is bounded, reproducible and retains locators.
            result = {
                "title": doc.title,
                "mode": "extractive",
                "truncated": doc.active_build.chunk_count > 8 if doc.active_build else False,
                "version": doc.active_build.version.version_no if doc.active_build else None,
                "sources": [
                    {"chunk_id": str(c.pk), "text": c.content[:500], "locator": c.locator} for c in chunks
                ],
            }
        else:
            rows = BusinessIssue.objects.filter(
                tenant_id=ctx.tenant_id,
                occurred_at__date__gte=params["date_from"],
                occurred_at__date__lte=params["date_to"],
            )
            if params.get("product"):
                rows = rows.filter(product=params["product"])
            group = params["group_by"]
            if group == "month":
                rows = rows.annotate(month=TruncMonth("occurred_at"))
            groups = list(rows.order_by().values(group).annotate(count=Count("id")).order_by(group)[:200])
            result = {
                "groups": groups,
                "scope": "当前租户演示问题，按 occurred_at UTC 日期（含首尾）计数",
                "date_from": str(params["date_from"]),
                "date_to": str(params["date_to"]),
            }
        if time.monotonic() - started > 10:
            raise ValidationError("工具执行超时")
        if not Tenant.objects.filter(pk=ctx.tenant_id, acl_epoch=ctx.acl_epoch, is_active=True).exists():
            raise ValidationError("权限已变更，已丢弃结果")
        call.status, call.result_summary = "completed", {"keys": list(result)}
        audit(ctx, "tool.execute", call)
        return result
    except Exception:
        call.status = "failed"
        raise
    finally:
        call.latency_ms = int((time.monotonic() - started) * 1000)
        call.save()
