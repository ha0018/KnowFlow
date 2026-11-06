from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from common.context import RequestContext

from .services import SCHEMAS, execute


class ToolInput(serializers.Serializer):
    tool = serializers.ChoiceField(choices=list(SCHEMAS))
    args = serializers.DictField()


class ToolsView(APIView):
    def get(self, request):
        return Response({"tools": list(SCHEMAS), "mode": "explicit-read-only", "max_calls_per_request": 1})

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
