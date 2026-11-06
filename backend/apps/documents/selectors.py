from django.db.models import F, Q
from django.shortcuts import get_object_or_404

from apps.knowledge.selectors import LEVELS, knowledge_bases, subject_q

from .models import Document, DocumentChunk


def documents(ctx, level="read"):
    qs = Document.objects.filter(
        tenant_id=ctx.tenant_id,
        deleted_at__isnull=True,
        knowledge_base_id__in=knowledge_bases(ctx, level).values("id"),
    )
    if ctx.user.is_tenant_admin:
        return qs
    return qs.filter(
        Q(acl_mode="inherit") | (subject_q(ctx, "acl__") & Q(acl__access_level__in=LEVELS[level]))
    ).distinct()


def get_document(ctx, pk, level="read"):
    return get_object_or_404(
        documents(ctx, level).select_related("active_build__version", "desired_build"), pk=pk
    )


def active_chunks(ctx, kb_ids=None):
    qs = DocumentChunk.objects.filter(
        tenant_id=ctx.tenant_id,
        document_id__in=documents(ctx).values("id"),
        build_id=F("document__active_build_id"),
        build__status="READY",
    )
    if kb_ids is not None:
        qs = qs.filter(knowledge_base_id__in=kb_ids)
    return qs


def chunk_readable(ctx, chunk_id):
    # Historical references may point at older builds, but permissions are current.
    return DocumentChunk.objects.filter(
        pk=chunk_id, tenant_id=ctx.tenant_id, document_id__in=documents(ctx).values("id")
    ).exists()
