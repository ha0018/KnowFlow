from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.permissions import require_capability
from common.context import RequestContext

from .configuration import revision, snapshot
from .corpus import verify_frozen
from .metrics import aggregate
from .models import EvalDataset, EvalResult, EvalRun
from .reporting import result_data, run_report
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
                    "splits": {
                        s: d.cases.filter(split=s).count()
                        for s in d.cases.values_list("split", flat=True).distinct()
                    },
                    "corpus_manifest_hash": d.corpus_manifest_hash,
                }
                for d in EvalDataset.objects.filter(tenant_id=ctx.tenant_id)
            ]
        )


class EvalInput(serializers.Serializer):
    dataset_id = serializers.UUIDField()
    mode = serializers.ChoiceField(choices=["dense", "hybrid"], default="hybrid")
    baseline = serializers.ChoiceField(choices=["B0", "B1", "B2"], required=False)
    split = serializers.ChoiceField(choices=["development", "tuning", "holdout"], required=False)
    answers = serializers.BooleanField(default=False)
    rewrite = serializers.BooleanField(default=False)


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
        try:
            verify_frozen(dataset)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from None
        data = serializer.validated_data
        split = data.get("split", "tuning" if dataset.split == "stratified" else "development")
        if not dataset.cases.filter(split=split).exists():
            raise serializers.ValidationError("所选分集中没有题目")
        name = data.get("baseline", "B0" if data["mode"] == "dense" else "B1")
        with transaction.atomic():
            run = EvalRun.objects.create(
                tenant_id=ctx.tenant_id,
                actor=ctx.user,
                dataset=dataset,
                trace_id=ctx.trace_id,
                config_snapshot=snapshot(dataset, name, split, data["answers"], data["rewrite"]),
                code_revision=revision(),
            )
            transaction.on_commit(lambda: dispatch_evaluation(run.pk))
        return Response({"id": run.pk, "status": "PENDING"}, status=202)


def owned_run(ctx, run_id):
    require_capability(ctx, "evaluation.run")
    return get_object_or_404(EvalRun, pk=run_id, tenant_id=ctx.tenant_id, actor=ctx.user)


class EvalResultsView(APIView):
    def get(self, request, run_id):
        ctx = RequestContext.from_request(request)
        return Response(run_report(ctx, owned_run(ctx, run_id)))


class ReviewInput(serializers.Serializer):
    answer_pass = serializers.BooleanField(allow_null=True, default=None)
    refusal_correct = serializers.BooleanField(allow_null=True, default=None)
    facts_total = serializers.IntegerField(min_value=0, max_value=1000, default=0)
    facts_supported = serializers.IntegerField(min_value=0, max_value=1000, default=0)
    cited_claims_total = serializers.IntegerField(min_value=0, max_value=1000, default=0)
    cited_claims_supported = serializers.IntegerField(min_value=0, max_value=1000, default=0)
    notes = serializers.CharField(max_length=4000, allow_blank=True, default="")

    def validate(self, data):
        if (
            data["facts_supported"] > data["facts_total"]
            or data["cited_claims_supported"] > data["cited_claims_total"]
        ):
            raise serializers.ValidationError("支撑数量不能超过复核总数")
        if not data["notes"].strip():
            raise serializers.ValidationError("请记录复核依据")
        return data


class EvalReviewView(APIView):
    def put(self, request, run_id, result_id):
        ctx = RequestContext.from_request(request)
        run = owned_run(ctx, run_id)
        if not run.config_snapshot.get("answers") or run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            raise serializers.ValidationError("仅可复核已完成的回答评测")
        serializer = ReviewInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            run = EvalRun.objects.select_for_update().get(pk=run.pk)
            row = get_object_or_404(
                EvalResult.objects.select_for_update().select_related("case", "run__actor", "run__dataset"),
                pk=result_id,
                run=run,
                tenant_id=ctx.tenant_id,
            )
            if row.error or result_data(ctx, row)["redacted"]:
                raise serializers.ValidationError("失败或已撤权的结果不能复核")
            row.human_review = {
                **serializer.validated_data,
                "reviewer_id": str(ctx.user.pk),
                "reviewed_at": timezone.now().isoformat(),
            }
            row.save(update_fields=["human_review"])
            cases = list(run.dataset.cases.filter(split=run.config_snapshot["split"]))
            run.metrics = aggregate(
                list(run.results.all()), cases, provider=run.metrics.get("provider", "unknown")
            )
            run.save(update_fields=["metrics"])
        return Response({"human_review": row.human_review, "metrics": run.metrics})
