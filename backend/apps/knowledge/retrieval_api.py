from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from ai_engine.retrieval import search
from common.context import RequestContext


class SearchInput(serializers.Serializer):
    query = serializers.CharField(max_length=2000)
    kb_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    top_k = serializers.IntegerField(min_value=1, max_value=8, default=5)


class SearchView(APIView):
    def post(self, request):
        serializer = SearchInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return Response(
            {
                "sources": search(
                    RequestContext.from_request(request),
                    data["query"],
                    data.get("kb_ids") or None,
                    data["top_k"],
                )
            }
        )
