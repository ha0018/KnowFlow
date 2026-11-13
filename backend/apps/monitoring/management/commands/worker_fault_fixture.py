"""Disposable fault-injection fixture; never accepts an ordinary tenant's task."""

import json
import uuid
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai_engine.gateway.config import profile
from apps.documents.models import DocumentTask
from apps.documents.services import upload
from apps.knowledge.models import KnowledgeBase
from apps.users.models import Tenant, User
from common.context import RequestContext


class Command(BaseCommand):
    help = "Create/read/expire only explicitly disposable S5 worker interruption fixtures"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["prepare", "status", "expire"])
        parser.add_argument("--disposable", action="store_true", required=True)
        parser.add_argument("--task-id")

    def handle(self, **options):
        if profile("embedding").provider != "demo":
            raise CommandError("Demo provider required")
        if options["action"] == "prepare":
            nonce = uuid.uuid4().hex
            tenant = Tenant.objects.create(name="S5 worker interruption", slug="s5-worker-" + nonce)
            user = User.objects.create_user(
                f"worker-{nonce}@disposable.test", tenant=tenant, is_tenant_admin=True
            )
            kb = KnowledgeBase.objects.create(tenant=tenant, owner=user, name="S5 interrupted document")
            raw = "\n\n".join(f"恢复条款 {i}：退款申请须在30天内提交。" for i in range(500)).encode()
            _, _, _, task = upload(
                RequestContext.for_user(user), kb.pk, SimpleUploadedFile("interrupted.md", raw)
            )
        else:
            task = DocumentTask.objects.select_related("tenant", "build__version__document").get(
                pk=options["task_id"], tenant__slug__startswith="s5-worker-"
            )
            if options["action"] == "expire":
                if task.status != "RUNNING":
                    raise CommandError("Fixture must be interrupted while RUNNING")
                # Force lease expiry so a CI drill does not spend six minutes idle.
                DocumentTask.objects.filter(pk=task.pk).update(
                    lease_until=timezone.now() - timedelta(seconds=1)
                )
        task.refresh_from_db()
        doc = task.build.version.document
        doc.refresh_from_db()
        self.stdout.write(
            json.dumps(
                {
                    "task_id": str(task.pk),
                    "status": task.status,
                    "stage": task.stage,
                    "attempt": task.attempt,
                    "fence_token": task.fence_token,
                    "chunks": task.build.chunks.count(),
                    "expected_chunks": task.build.chunk_count,
                    "active_build": str(doc.active_build_id) if doc.active_build_id else None,
                    "build_id": str(task.build_id),
                    "error_code": task.error_code,
                }
            )
        )
