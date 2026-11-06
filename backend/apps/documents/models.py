from django.conf import settings
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Q
from pgvector.django import VectorField

from apps.knowledge.models import ACLBase
from common.models import TenantEntity


class Document(TenantEntity):
    knowledge_base = models.ForeignKey(
        "knowledge.KnowledgeBase", related_name="documents", on_delete=models.PROTECT
    )
    title = models.CharField(max_length=240)
    acl_mode = models.CharField(
        max_length=12, default="inherit", choices=[("inherit", "继承"), ("restricted", "受限")]
    )
    active_build = models.ForeignKey(
        "IndexBuild", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    desired_build = models.ForeignKey(
        "IndexBuild", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantEntity.Meta):
        indexes = [models.Index(fields=["tenant", "knowledge_base", "created_at", "id"])]


class DocumentACL(ACLBase):
    document = models.ForeignKey(Document, related_name="acl", on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False, role__isnull=True) | Q(user__isnull=True, role__isnull=False)
                ),
                name="doc_acl_one_subject",
            ),
            models.UniqueConstraint(fields=["document", "user"], name="doc_acl_user"),
            models.UniqueConstraint(fields=["document", "role"], name="doc_acl_role"),
        ]


class DocumentVersion(TenantEntity):
    document = models.ForeignKey(Document, related_name="versions", on_delete=models.PROTECT)
    version_no = models.PositiveIntegerField()
    file = models.FileField(max_length=500)
    original_name = models.CharField(max_length=240)
    sha256 = models.CharField(max_length=64)
    mime_type = models.CharField(max_length=120)
    size_bytes = models.PositiveIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta(TenantEntity.Meta):
        constraints = [
            models.UniqueConstraint(fields=["document", "version_no"], name="unique_document_version")
        ]


class IndexBuild(TenantEntity):
    version = models.ForeignKey(DocumentVersion, related_name="builds", on_delete=models.PROTECT)
    pipeline_hash = models.CharField(max_length=64)
    config_snapshot = models.JSONField(default=dict)
    embedding_profile = models.CharField(max_length=240)
    status = models.CharField(max_length=20, default="PENDING")
    chunk_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)
    parsed_blocks = models.JSONField(default=list)

    class Meta(TenantEntity.Meta):
        constraints = [
            models.UniqueConstraint(fields=["version", "pipeline_hash"], name="unique_version_pipeline")
        ]


class DocumentChunk(TenantEntity):
    knowledge_base = models.ForeignKey("knowledge.KnowledgeBase", on_delete=models.PROTECT)
    document = models.ForeignKey(Document, on_delete=models.PROTECT)
    build = models.ForeignKey(IndexBuild, related_name="chunks", on_delete=models.CASCADE)
    ordinal = models.PositiveIntegerField()
    content = models.TextField()
    content_hash = models.CharField(max_length=64)
    token_count = models.PositiveIntegerField()
    embedding = VectorField(dimensions=settings.EMBEDDING_DIMENSIONS)
    search_tokens = models.TextField()
    search_vector = SearchVectorField(null=True)
    locator = models.JSONField(default=dict)

    class Meta(TenantEntity.Meta):
        constraints = [models.UniqueConstraint(fields=["build", "ordinal"], name="unique_build_chunk")]
        indexes = [models.Index(fields=["tenant", "knowledge_base", "build"])]


class DocumentTask(TenantEntity):
    build = models.OneToOneField(IndexBuild, related_name="task", on_delete=models.CASCADE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    celery_task_id = models.CharField(max_length=64, blank=True)
    trace_id = models.CharField(max_length=64)
    stage = models.CharField(max_length=20, default="PENDING")
    status = models.CharField(max_length=20, default="PENDING")
    attempt = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True)
    fence_token = models.PositiveIntegerField(default=0)
    queued_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=80, blank=True)

    class Meta(TenantEntity.Meta):
        indexes = [models.Index(fields=["status", "lease_until"])]
