from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.permissions import require_capability
from common.context import RequestContext

from .evidence import analysis_reference
from .models import ToolCall
from .schemas import SCHEMAS, StrictSerializer, tool_definitions
from .services import execute


class ToolInput(StrictSerializer):
    tool = serializers.ChoiceField(choices=list(SCHEMAS))
    args = serializers.DictField()


class ToolsView(APIView):
    def get(self, request):
        return Response(
            {
                "tools": list(SCHEMAS),
                "schemas": tool_definitions(),
                "mode": "explicit-read-only",
                "max_calls_per_request": 1,
            }
        )

    def post(self, request):
        serializer = ToolInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            execute(
                RequestContext.from_request(request),
                serializer.validated_data["tool"],
                serializer.validated_data["args"],
            )
        )


class AgentReferenceView(APIView):
    def get(self, request, call_id):
        return Response(analysis_reference(RequestContext.from_request(request), call_id))


class AgentCallsView(APIView):
    def get(self, request):
        ctx = RequestContext.from_request(request)
        require_capability(ctx, "agent.execute")
        return Response(
            list(
                ToolCall.objects.filter(tenant_id=ctx.tenant_id, actor=ctx.user).values(
                    "id",
                    "run_id",
                    "step_index",
                    "tool_name",
                    "status",
                    "latency_ms",
                    "error_code",
                    "args_redacted",
                    "trace_id",
                    "created_at",
                )[:100]
            )
        )
