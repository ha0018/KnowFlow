import hashlib
import json
from datetime import timedelta

from rest_framework import serializers
from rest_framework.exceptions import ValidationError


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise ValidationError("参数包含未知字段或格式错误")
        return super().to_internal_value(data)


class SearchArgs(StrictSerializer):
    query = serializers.CharField(max_length=2000)
    kb_ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=20)
    top_k = serializers.IntegerField(min_value=1, max_value=8, default=5)


class SummaryArgs(StrictSerializer):
    document_id = serializers.UUIDField()
    focus = serializers.CharField(max_length=500, required=False, default="", allow_blank=True)


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


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("non-finite JSON")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def validate_args(name, args):
    if name not in SCHEMAS:
        raise ValidationError("未知工具")
    if isinstance(args, str):
        if len(args.encode()) > 8000:
            raise ValidationError("参数过大")
        try:
            args = strict_json(args)
        except (ValueError, TypeError):
            raise ValidationError("无效 JSON 参数") from None
    if not isinstance(args, dict) or set(args) - set(SCHEMAS[name]().fields):
        raise ValidationError("参数包含未知字段或格式错误")
    args = dict(args)
    optional = {
        "KnowledgeSearchTool": {"kb_ids", "top_k"},
        "DocumentSummaryTool": {"focus"},
        "DataAnalysisTool": {"product"},
    }[name]
    for key in optional:
        if args.get(key) is None:
            args.pop(key, None)
    for key, value in args.items():
        expected = list if key == "kb_ids" else int if key == "top_k" else str
        if type(value) is not expected:
            raise ValidationError("参数 JSON 类型不正确")
        if key == "kb_ids" and (any(type(v) is not str for v in value) or len(value) != len(set(value))):
            raise ValidationError("知识库 ID 必须唯一且为字符串")
    serializer = SCHEMAS[name](data=args)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def redacted(params):
    result = {}
    for key, value in params.items():
        if key in {"query", "focus", "product"}:
            result[key + "_sha256"] = hashlib.sha256(str(value).encode()).hexdigest()
            result[key + "_length"] = len(value)
        elif key == "kb_ids":
            result[key] = list(map(str, value))
        else:
            result[key] = str(value) if key != "top_k" else value
    return result


def tool_definitions():
    definitions = [
        (
            "KnowledgeSearchTool",
            "检索当前授权知识库，返回带版本与来源的证据。",
            {
                "query": {"type": "string"},
                "kb_ids": {"type": ["array", "null"], "items": {"type": "string"}},
                "top_k": {"type": ["integer", "null"]},
            },
        ),
        (
            "DocumentSummaryTool",
            "读取一份已授权文档的有界原文摘要，可指定重点。",
            {"document_id": {"type": "string"}, "focus": {"type": ["string", "null"]}},
        ),
        (
            "DataAnalysisTool",
            "按 UTC 日期（含首尾）统计当前租户演示问题，最多366天。",
            {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "product": {"type": ["string", "null"]},
                "group_by": {"type": "string", "enum": ["month", "product", "category"]},
            },
        ),
    ]
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": fields,
                    "required": list(fields),
                    "additionalProperties": False,
                },
            },
        }
        for name, description, fields in definitions
    ]
