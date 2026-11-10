from django.db import models

from common.models import TenantEntity


class EvalDataset(TenantEntity):
    name = models.CharField(max_length=120)
    version = models.CharField(max_length=40)
    corpus_manifest_hash = models.CharField(max_length=64)
    split = models.CharField(max_length=20, default="development")
    manifest = models.JSONField(default=dict)

    class Meta(TenantEntity.Meta):
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name", "version"], name="unique_dataset_version")
        ]


class EvalCase(TenantEntity):
    dataset = models.ForeignKey(EvalDataset, related_name="cases", on_delete=models.CASCADE)
    question = models.CharField(max_length=2000)
    expected_answer = models.TextField(blank=True)
    gold_evidence = models.JSONField(default=list)
    category = models.CharField(max_length=40, default="answerable")
    actor_fixture = models.CharField(max_length=120, blank=True)
    case_key = models.CharField(max_length=80, blank=True)
    split = models.CharField(max_length=20, default="development")
    spec = models.JSONField(default=dict)


class EvalRun(TenantEntity):
    dataset = models.ForeignKey(EvalDataset, on_delete=models.PROTECT)
    actor = models.ForeignKey("users.User", on_delete=models.PROTECT)
    config_snapshot = models.JSONField(default=dict)
    code_revision = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, default="PENDING")
    metrics = models.JSONField(default=dict)
    trace_id = models.CharField(max_length=64)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)


class EvalResult(TenantEntity):
    run = models.ForeignKey(EvalRun, related_name="results", on_delete=models.CASCADE)
    case = models.ForeignKey(EvalCase, on_delete=models.PROTECT)
    retrieved_ids = models.JSONField(default=list)
    metrics = models.JSONField(default=dict)
    error = models.CharField(max_length=80, blank=True)
    human_review = models.JSONField(default=dict)
    retrieval_snapshot = models.JSONField(default=list)
    answer = models.TextField(blank=True)
    trace = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "case"], name="unique_eval_case_result")]
