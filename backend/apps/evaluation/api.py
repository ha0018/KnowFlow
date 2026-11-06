from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.permissions import require_capability
from common.context import RequestContext

from .models import EvalDataset, EvalRun
from .services import dispatch_evaluation


class DatasetsView(APIView):
    def get(self, request):
        ctx = RequestContext.from_request(request)
        require_capability(ctx, "evaluation.run")
        return Response(
            [
                {
                    "id": d.pk,
                    "name": d.name,
                    "version": d.version,
                    "split": d.split,
                    "case_count": d.cases.count(),
                }
                for d in EvalDataset.objects.filter(tenant_id=ctx.tenant_id)
            ]
        )


class EvalInput(serializers.Serializer):
    dataset_id = serializers.UUIDField()
    mode = serializers.ChoiceField(choices=["dense", "hybrid"], default="hybrid")


class EvalRunsView(APIView):
    def get(self, request):
        ctx = RequestContext.from_request(request)
        require_capability(ctx, "evaluation.run")
        return Response(
            list(
                EvalRun.objects.filter(tenant_id=ctx.tenant_id, actor=ctx.user).values(
                    "id", "status", "metrics", "config_snapshot", "created_at"
                )[:30]
            )
        )

    def post(self, request):
        ctx = RequestContext.from_request(request)
        require_capability(ctx, "evaluation.run")
        serializer = EvalInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        dataset = get_object_or_404(
            EvalDataset, pk=serializer.validated_data["dataset_id"], tenant_id=ctx.tenant_id
        )
        with transaction.atomic():
            run = EvalRun.objects.create(
                tenant_id=ctx.tenant_id,
                actor=ctx.user,
                dataset=dataset,
                trace_id=ctx.trace_id,
                config_snapshot={
                    "mode": serializer.validated_data["mode"],
                    "embedding_profile": settings.EMBEDDING_PROFILE,
                    "retrieval": "rrf-v1",
                    "corpus_manifest_hash": dataset.corpus_manifest_hash,
                    "dataset_version": dataset.version,
                },
            )
            transaction.on_commit(lambda: dispatch_evaluation(run.pk))
        return Response({"id": run.pk, "status": "PENDING"}, status=202)
