from dataclasses import replace

from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from ai_engine.retrieval import search
from ai_engine.retrieval.options import defaults
from common.context import RequestContext


class SearchInput(serializers.Serializer):
    query = serializers.CharField(max_length=2000)
    kb_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    top_k = serializers.IntegerField(min_value=1, max_value=8, default=5)
    mode = serializers.ChoiceField(choices=["dense", "hybrid"], required=False)
    rerank = serializers.BooleanField(required=False)
    rewrite = serializers.BooleanField(required=False)


class SearchView(APIView):
    def post(self, request):
        serializer = SearchInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        diagnostics = {}
        options = replace(
            defaults(), **{key: data[key] for key in ("mode", "rerank", "rewrite") if key in data}
        )
        return Response(
            {
                "sources": search(
                    RequestContext.from_request(request),
                    data["query"],
                    data.get("kb_ids") or None,
                    data["top_k"],
                    options=options,
                    diagnostics=diagnostics,
                ),
                "diagnostics": diagnostics,
            }
        )
