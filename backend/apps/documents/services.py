import hashlib
import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ai_engine.chunking import split_blocks
from ai_engine.embeddings import tokenize
from ai_engine.gateway import embed
from ai_engine.gateway.config import GatewayError
from ai_engine.parsers import ParseError, parse, validate_file
from apps.knowledge.selectors import get_knowledge_base
from apps.users.models import Tenant
from common.context import RequestContext, audit, reload_context

from .models import Document, DocumentChunk, DocumentTask, DocumentVersion, IndexBuild, StorageObject
from .selectors import get_document

logger = logging.getLogger(__name__)


def pipeline_config():
    return {
        "parser": "v1",
        "chunker": "proxy-token-v1",
        "size": 600,
        "overlap": 80,
        "embedding_profile": settings.EMBEDDING_PROFILE,
        "dictionary": "jieba-search-v1",
    }


def create_build(ctx, version):
    config = pipeline_config()
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    build, _ = IndexBuild.objects.get_or_create(
        tenant_id=ctx.tenant_id,
        version=version,
        pipeline_hash=digest,
        defaults={"config_snapshot": config, "embedding_profile": settings.EMBEDDING_PROFILE},
    )
    task, _ = DocumentTask.objects.get_or_create(
        tenant_id=ctx.tenant_id, build=build, defaults={"actor": ctx.user, "trace_id": ctx.trace_id}
    )
    Document.objects.filter(pk=version.document_id).update(desired_build=build)
    return build, task


def upload(ctx, kb_id, uploaded, title=None, document_id=None):
    kb = get_knowledge_base(ctx, kb_id, "edit")
    raw = uploaded.read(settings.MAX_UPLOAD_BYTES + 1)
    name = Path(uploaded.name.replace("\\", "/")).name[:240]
    try:
        ext, mime = validate_file(name, raw)
    except ParseError as exc:
        raise ValidationError({"file": str(exc)}) from exc
    doc = get_document(ctx, document_id, "edit") if document_id else None
    if doc and doc.knowledge_base_id != kb.pk:
        raise ValidationError("文档不属于此知识库")
    key = f"tenants/{ctx.tenant_id}/{uuid.uuid4().hex}{ext}"
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
        ctx = reload_context(ctx)
        get_knowledge_base(ctx, kb_id, "edit")
        storage_object = StorageObject.objects.create(
            tenant_id=ctx.tenant_id,
            key=key,
            lease_until=timezone.now() + timedelta(hours=1),
        )
    key = default_storage.save(key, ContentFile(raw))
    try:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
            ctx = reload_context(ctx)
            get_knowledge_base(ctx, kb_id, "edit")
            storage_object.refresh_from_db()
            if storage_object.state != "writing" or storage_object.lease_until <= timezone.now():
                raise ValidationError("上传租约已过期，请重新提交")
            storage_object.key, storage_object.state, storage_object.lease_until = key, "available", None
            storage_object.save(update_fields=["key", "state", "lease_until", "updated_at"])
            if doc:
                doc = get_document(ctx, doc.pk, "edit")
                Document.objects.select_for_update().get(pk=doc.pk)
            else:
                doc = Document.objects.create(tenant_id=ctx.tenant_id, knowledge_base=kb, title=title or name)
            number = (doc.versions.aggregate(n=Max("version_no"))["n"] or 0) + 1
            version = DocumentVersion.objects.create(
                tenant_id=ctx.tenant_id,
                document=doc,
                version_no=number,
                file=key,
                original_name=name,
                sha256=hashlib.sha256(raw).hexdigest(),
                mime_type=mime,
                size_bytes=len(raw),
                created_by=ctx.user,
            )
            build, task = create_build(ctx, version)
            audit(ctx, "document.upload", doc)
            transaction.on_commit(lambda: dispatch(task.pk))
    except Exception:
        if not DocumentVersion.objects.filter(file=key).exists():
            default_storage.delete(key)
            StorageObject.objects.filter(pk=storage_object.pk).update(state="deleted", lease_until=None)
        raise
    return doc, version, build, task


def dispatch(task_id):
    from .tasks import process_document

    try:
        result = process_document.delay(str(task_id))
        DocumentTask.objects.filter(pk=task_id).update(celery_task_id=result.id, queued_at=timezone.now())
    except Exception:
        # The DB outbox remains pending; periodic recovery will redeliver it.
        logger.warning("document_dispatch_failed task_id=%s", task_id)


@transaction.atomic
def claim(task_id):
    tenant_id = DocumentTask.objects.values_list("tenant_id", flat=True).get(pk=task_id)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    task = (
        DocumentTask.objects.select_for_update()
        .select_related("build__version__document", "actor__tenant")
        .get(pk=task_id)
    )
    if task.status in {"READY", "FAILED", "CANCELLED"} or (
        task.lease_until and task.lease_until > timezone.now()
    ):
        return None
    task.fence_token += 1
    task.attempt += 1
    task.status = "RUNNING"
    task.lease_until = timezone.now() + timedelta(minutes=6)
    task.save()
    return task


def check_task(task, fence):
    from apps.documents.selectors import documents

    current = DocumentTask.objects.get(pk=task.pk)
    ctx = RequestContext.for_user(current.actor, current.trace_id)
    if current.fence_token != fence or current.status != "RUNNING" or current.lease_until <= timezone.now():
        raise GatewayError("STALE_WORKER")
    if (
        not current.actor.is_active
        or not current.actor.tenant.is_active
        or not documents(ctx, "edit")
        .filter(pk=task.build.version.document_id, desired_build_id=task.build_id)
        .exists()
    ):
        raise GatewayError("DOCUMENT_UNAVAILABLE")
    return ctx


@transaction.atomic
def stage(task, fence, name):
    Tenant.objects.select_for_update().get(pk=task.tenant_id)
    DocumentTask.objects.select_for_update().get(pk=task.pk)
    check_task(task, fence)
    DocumentTask.objects.filter(pk=task.pk).update(
        stage=name, lease_until=timezone.now() + timedelta(minutes=6)
    )
    IndexBuild.objects.filter(pk=task.build_id).update(status=name)


def process(task_id):
    task = claim(task_id)
    if task is None:
        return
    fence = task.fence_token
    build = task.build
    try:
        stage(task, fence, "PARSING")
        with build.version.file.open("rb") as source:
            raw = source.read(settings.MAX_UPLOAD_BYTES + 1)
        blocks = parse(build.version.original_name, raw, check=lambda: check_task(task, fence))
        stage(task, fence, "CHUNKING")
        chunks = split_blocks(blocks, build.config_snapshot["size"], build.config_snapshot["overlap"])
        stage(task, fence, "EMBEDDING")
        if build.embedding_profile != settings.EMBEDDING_PROFILE:
            raise ParseError("EMBEDDING_PROFILE_CHANGED")
        for start in range(0, len(chunks), 16):
            ctx = check_task(task, fence)
            batch = chunks[start : start + 16]
            vectors, _ = embed(
                [c["content"] for c in batch],
                ctx=ctx,
                task=task,
                before_request=lambda: check_task(task, fence),
            )
            with transaction.atomic():
                Tenant.objects.select_for_update().get(pk=task.tenant_id)
                DocumentTask.objects.select_for_update().get(pk=task.pk)
                check_task(task, fence)
                for data, vector in zip(batch, vectors):
                    chunk, _ = DocumentChunk.objects.update_or_create(
                        build=build,
                        ordinal=data["ordinal"],
                        defaults={
                            **data,
                            "tenant_id": task.tenant_id,
                            "knowledge_base_id": build.version.document.knowledge_base_id,
                            "document_id": build.version.document_id,
                            "embedding": vector,
                            "search_tokens": " ".join(tokenize(data["content"])),
                        },
                    )
                    if connection.vendor == "postgresql":
                        from django.contrib.postgres.search import SearchVector
                        from django.db.models import Value

                        DocumentChunk.objects.filter(pk=chunk.pk).update(
                            search_vector=SearchVector(Value(chunk.search_tokens), config="simple")
                        )
                DocumentTask.objects.filter(pk=task.pk).update(
                    lease_until=timezone.now() + timedelta(minutes=6)
                )
        stage(task, fence, "INDEXING")
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=task.tenant_id)
            DocumentTask.objects.select_for_update().get(pk=task.pk)
            ctx = check_task(task, fence)
            doc = Document.objects.select_for_update().get(pk=build.version.document_id)
            if build.chunks.count() != len(chunks) or not chunks:
                raise ParseError("CHUNK_COUNT_MISMATCH")
            IndexBuild.objects.filter(pk=build.pk).update(
                status="READY", parsed_blocks=blocks, chunk_count=len(chunks), error_code=""
            )
            if doc.desired_build_id == build.pk and doc.deleted_at is None:
                doc.active_build_id = build.pk
                doc.save(update_fields=["active_build"])
                audit(ctx, "document.publish", doc)
            DocumentTask.objects.filter(pk=task.pk, fence_token=fence).update(
                status="READY", stage="READY", finished_at=timezone.now(), lease_until=None, error_code=""
            )
    except Exception as exc:
        if str(exc) == "STALE_WORKER":
            return
        cancelled = str(exc) == "DOCUMENT_UNAVAILABLE"
        transient = (
            (isinstance(exc, GatewayError) and exc.retryable)
            or isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError))
            and (
                not isinstance(exc, httpx.HTTPStatusError)
                or exc.response.status_code in {408, 429, 500, 502, 503, 504}
            )
        )
        state = "CANCELLED" if cancelled else ("RETRY_WAIT" if transient and task.attempt < 3 else "FAILED")
        from apps.monitoring.metering import QuotaExceeded

        code = (
            "QUOTA_EXCEEDED"
            if isinstance(exc, QuotaExceeded)
            else str(exc)
            if isinstance(exc, (ParseError, ValueError))
            else "PROCESSING_ERROR"
        )
        # Every failure update is fenced too, so a late worker cannot overwrite a successor.
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=task.tenant_id)
            current = DocumentTask.objects.select_for_update().get(pk=task.pk)
            if current.fence_token != fence:
                return
            current.status, current.error_code = state, code[:80]
            current.lease_until = (
                timezone.now() + timedelta(seconds=30 * 2**task.attempt) if state == "RETRY_WAIT" else None
            )
            current.finished_at = None if state == "RETRY_WAIT" else timezone.now()
            current.save()
            IndexBuild.objects.filter(pk=build.pk).update(status=state, error_code=code[:80])
        logger.warning(
            "document_process_failed trace_id=%s task_id=%s code=%s", task.trace_id, task.pk, code[:80]
        )


@transaction.atomic
def reindex(ctx, document_id):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    doc = get_document(ctx, document_id, "edit")
    Document.objects.select_for_update().get(pk=doc.pk)
    version = doc.versions.order_by("-version_no").first()
    build, task = create_build(ctx, version)
    if task.status in {"FAILED", "CANCELLED"}:
        task.status, task.lease_until, task.attempt = "PENDING", None, 0
        task.actor, task.trace_id = ctx.user, ctx.trace_id
        task.save()
    transaction.on_commit(lambda: dispatch(task.pk))
    return build, task
