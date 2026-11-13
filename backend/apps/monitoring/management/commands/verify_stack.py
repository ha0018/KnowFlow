"""Exercise a disposable deployed stack with real DB, queue, worker and storage.

This command creates isolated verification tenants. It never uses production
model credentials and reports IDs/metadata rather than passwords or document text.
"""

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlsplit

import httpx
import redis
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone
from kombu.exceptions import OperationalError

from apps.agents.models import BusinessIssue
from apps.conversations.models import Run
from apps.documents.models import Document, DocumentChunk, DocumentTask, DocumentVersion
from apps.documents.services import upload
from apps.monitoring.models import UsageReservation
from apps.users.models import Tenant, User
from common.context import RequestContext
from config.celery import app as celery_app


def ensure(condition, code):
    if not condition:
        raise AssertionError(code)


class StackVerifier:
    def __init__(self, base_url, output):
        self.client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(70, connect=5))
        self.output = Path(output)
        self.report = {
            "schema": "knowflow-infrastructure-v1",
            "started_at": timezone.now().isoformat(),
            "revision": os.environ.get("CODE_REVISION", "workspace"),
            "status": "running",
            "checks": [],
        }
        self.suffix = uuid.uuid4().hex[:12]

    def check(self, name, operation):
        started = time.monotonic()
        record = {"name": name, "status": "running"}
        self.report["checks"].append(record)
        try:
            record["details"] = operation() or {}
            record["status"] = "passed"
            print(f"PASS {name}", flush=True)
        except Exception as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            code = (
                str(exc)
                if isinstance(exc, AssertionError) and re.fullmatch(r"[A-Z0-9_]+", str(exc))
                else type(exc).__name__
            )
            record["error_code"] = code
            raise CommandError(f"Verification failed: {name} ({code})") from None
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000)

    def request(self, method, path, token=None, expected=200, **kwargs):
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = "Bearer " + token
        response = self.client.request(method, "/api/v1" + path, headers=headers, **kwargs)
        ensure(response.status_code == expected, f"HTTP_{method}_{response.status_code}_EXPECTED_{expected}")
        return response

    def wait_task(self, task_id, expected="READY", timeout=75):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = DocumentTask.objects.select_related("build").get(pk=task_id)
            if task.status == expected:
                return task
            ensure(task.status not in {"FAILED", "CANCELLED", "READY"}, "UNEXPECTED_TASK_TERMINAL")
            time.sleep(0.5)
        raise AssertionError("TASK_DEADLINE_EXCEEDED")

    def dependencies(self):
        ensure(connection.vendor == "postgresql", "POSTGRESQL_REQUIRED")
        ensure(
            not settings.CELERY_TASK_ALWAYS_EAGER and not celery_app.conf.task_always_eager,
            "INDEPENDENT_WORKER_REQUIRED",
        )
        ensure(settings.S3_ENABLED, "S3_STORAGE_REQUIRED")
        ensure(
            settings.AI_PROVIDER == "demo" and settings.EMBEDDING_PROVIDER == "demo", "DEMO_MODEL_REQUIRED"
        )
        response = self.client.get("/health/ready")
        ensure(response.status_code == 200, "STACK_NOT_READY")
        ensure(
            response.json()["dependencies"] == {"database": "ok", "redis": "ok", "object_storage": "ok"},
            "DEPENDENCY_NOT_VERIFIED",
        )
        queue = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2)
        ensure(queue.ping(), "REDIS_NOT_READY")
        workers = celery_app.control.ping(timeout=5)
        ensure(workers, "NO_WORKER_RESPONDED")
        with connection.cursor() as cursor:
            cursor.execute("SELECT version()")
            database_version = cursor.fetchone()[0]
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            vector_version = cursor.fetchone()[0]
        return {
            "database": database_version,
            "pgvector": vector_version,
            "redis": queue.info("server")["redis_version"],
            "worker_count": len(workers),
            "model": "demo",
        }

    def database_permissions(self):
        ensure("POSTGRES_PASSWORD" not in os.environ, "MIGRATION_SECRET_IN_APPLICATION_ENV")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname=current_user"
            )
            role, superuser, createdb, createrole, bypass = cursor.fetchone()
            ensure(
                role == "knowflow_app" and not any([superuser, createdb, createrole, bypass]),
                "APPLICATION_ROLE_IS_PRIVILEGED",
            )
            cursor.execute("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            ensure(not cursor.fetchone()[0], "APPLICATION_CAN_CHANGE_SCHEMA")
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"CREATE TABLE public.verify_{self.suffix} (id integer)")
                raise AssertionError("APPLICATION_DDL_SUCCEEDED")
        except DatabaseError as exc:
            ensure(getattr(exc.__cause__, "sqlstate", None) == "42501", "UNEXPECTED_DDL_FAILURE")
        return {"application_role": role, "schema_create_denied": True}

    def fixtures(self):
        password = secrets.token_urlsafe(24)
        self.tenant = Tenant.objects.create(name="Infrastructure verification", slug="verify-" + self.suffix)
        other = Tenant.objects.create(name="Isolation verification", slug="verify-other-" + self.suffix)
        self.admin = User.objects.create_user(
            f"admin-{self.suffix}@verify.invalid", password, tenant=self.tenant, is_tenant_admin=True
        )
        self.member = User.objects.create_user(
            f"member-{self.suffix}@verify.invalid", password, tenant=self.tenant
        )
        outsider = User.objects.create_user(
            f"outsider-{self.suffix}@verify.invalid", password, tenant=other, is_tenant_admin=True
        )
        for attr, user in [
            ("admin_token", self.admin),
            ("member_token", self.member),
            ("outsider_token", outsider),
        ]:
            data = self.request(
                "POST", "/auth/login", json={"email": user.email, "password": password}
            ).json()
            setattr(self, attr, data["access"])
        self.kb_id = self.request(
            "POST",
            "/knowledge-bases",
            self.admin_token,
            expected=201,
            json={"name": "Verification", "visibility": "private"},
        ).json()["id"]
        self.request(
            "POST",
            f"/knowledge-bases/{self.kb_id}/acl",
            self.admin_token,
            json=[{"user_id": str(self.member.pk), "access_level": "read"}],
        )
        try:
            with transaction.atomic():
                Document.objects.create(tenant=other, knowledge_base_id=self.kb_id, title="forbidden")
            raise AssertionError("CROSS_TENANT_INSERT_SUCCEEDED")
        except IntegrityError:
            pass
        return {
            "tenant_id": str(self.tenant.pk),
            "knowledge_base_id": self.kb_id,
            "cross_tenant_insert_denied": True,
        }

    def ingestion(self):
        self.marker = "来源标识" + self.suffix
        self.raw = f"# 设备报修制度\n\n设备报修请联系技术支持，受理时限为2个工作日。{self.marker}。".encode()
        identifiers = self.request(
            "POST",
            f"/knowledge-bases/{self.kb_id}/documents",
            self.admin_token,
            expected=202,
            files={"file": ("设备报修.md", self.raw, "text/markdown")},
        ).json()
        self.doc_id = identifiers["document_id"]
        self.task = self.wait_task(identifiers["task_id"])
        ensure(self.task.attempt == 1 and self.task.celery_task_id, "ASYNC_DELIVERY_NOT_RECORDED")
        self.active_build_id = self.task.build_id
        self.version = DocumentVersion.objects.get(pk=identifiers["version_id"])
        ensure(
            Document.objects.get(pk=self.doc_id).active_build_id == self.active_build_id,
            "BUILD_NOT_PUBLISHED",
        )
        chunks = DocumentChunk.objects.filter(build_id=self.active_build_id)
        ensure(chunks.exists() and not chunks.filter(search_vector__isnull=True).exists(), "FTS_NOT_INDEXED")
        ensure(
            all(len(c.embedding) == settings.EMBEDDING_DIMENSIONS for c in chunks),
            "VECTOR_DIMENSION_MISMATCH",
        )
        ensure(default_storage.exists(self.version.file.name), "OBJECT_NOT_STORED")
        with self.version.file.open("rb") as source:
            ensure(
                hashlib.sha256(source.read()).hexdigest() == self.version.sha256, "STORED_FILE_HASH_MISMATCH"
            )
        object_url = (
            settings.STORAGES["default"]["OPTIONS"]["endpoint_url"].rstrip("/")
            + "/"
            + quote(default_storage.bucket_name + "/" + self.version.file.name, safe="/")
        )
        ensure(httpx.get(object_url, timeout=5).status_code in {403, 404}, "OBJECT_IS_PUBLIC")
        downloaded = self.request("GET", f"/documents/{self.doc_id}/download", self.member_token)
        ensure(downloaded.content == self.raw, "AUTHORIZED_DOWNLOAD_MISMATCH")
        for path in [
            f"/documents/{self.doc_id}",
            f"/documents/{self.doc_id}/download",
            f"/tasks/{self.task.pk}",
        ]:
            self.request("GET", path, self.outsider_token, expected=404)
        return {
            "task_id": str(self.task.pk),
            "attempt": self.task.attempt,
            "chunk_count": chunks.count(),
            "private_object": True,
            "authorized_download": True,
        }

    def retrieval_and_stream(self):
        self.question = "设备报修的受理时限是多少？"
        hits = self.request(
            "POST",
            "/retrieval/search",
            self.member_token,
            json={"query": self.question, "kb_ids": [self.kb_id]},
        ).json()["sources"]
        ensure(any(h["document_id"] == self.doc_id for h in hits), "AUTHORIZED_RETRIEVAL_EMPTY")
        forbidden = self.request(
            "POST",
            "/retrieval/search",
            self.outsider_token,
            json={"query": self.question, "kb_ids": [self.kb_id]},
        ).json()["sources"]
        ensure(not forbidden, "CROSS_TENANT_RETRIEVAL_LEAK")
        self.conversation_id = self.request(
            "POST", "/conversations", self.member_token, expected=201, json={"kb_ids": [self.kb_id]}
        ).json()["id"]
        self.key = "verify-" + self.suffix
        path = f"/api/v1/conversations/{self.conversation_id}/messages:stream"
        events, text, references = [], "", []
        name = ""
        with self.client.stream(
            "POST",
            path,
            headers={"Authorization": "Bearer " + self.member_token, "Idempotency-Key": self.key},
            json={"question": self.question},
        ) as response:
            ensure(response.status_code == 200, "SSE_REQUEST_FAILED")
            ensure("text/event-stream" in response.headers.get("content-type", ""), "SSE_CONTENT_TYPE")
            # Nginx consumes X-Accel-Buffering internally rather than forwarding it.
            ensure("no-transform" in response.headers.get("cache-control", ""), "SSE_CACHE_POLICY")
            for line in response.iter_lines():
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    events.append(name)
                    if name == "meta":
                        self.run_id = data["run_id"]
                    elif name == "references":
                        references = data["sources"]
                    elif name == "delta":
                        text += data["text"]
                    elif name == "done":
                        ensure(data["status"] == "completed", "SSE_TERMINAL_NOT_COMPLETED")
        ensure(all(name in events for name in ["meta", "references", "delta", "done"]), "SSE_EVENTS_MISSING")
        ensure(self.marker in text and references, "SSE_EVIDENCE_MISSING")
        # A title chunk can rank first; verify the actual cited evidence rather
        # than assuming the first reference contains the document body.
        self.reference_id = None
        for candidate in references:
            reference = self.request("GET", f"/references/{candidate['id']}", self.member_token).json()
            if self.marker in reference["excerpt"]:
                self.reference_id = candidate["id"]
                break
        ensure(self.reference_id is not None, "REFERENCE_PREVIEW_MISMATCH")
        repeated = self.request(
            "POST",
            f"/conversations/{self.conversation_id}/messages:stream",
            self.member_token,
            headers={"Idempotency-Key": self.key},
            json={"question": self.question},
        ).json()
        ensure(repeated.get("replayed") and repeated["run_id"] == self.run_id, "RUN_NOT_IDEMPOTENT")
        ensure(Run.objects.filter(conversation_id=self.conversation_id).count() == 1, "DUPLICATE_RUN_CREATED")
        self.request(
            "GET", f"/conversations/{self.conversation_id}/messages", self.outsider_token, expected=404
        )
        return {
            "run_id": self.run_id,
            "reference_id": self.reference_id,
            "events": events,
            "idempotent": True,
            "through_nginx": True,
        }

    def revoked_history(self):
        self.request("POST", f"/knowledge-bases/{self.kb_id}/acl", self.admin_token, json=[])
        hits = self.request(
            "POST",
            "/retrieval/search",
            self.member_token,
            json={"query": self.question, "kb_ids": [self.kb_id]},
        ).json()["sources"]
        ensure(not hits, "REVOKED_DOCUMENT_RETRIEVABLE")
        self.request("GET", f"/references/{self.reference_id}", self.member_token, expected=404)
        messages = self.request(
            "GET", f"/conversations/{self.conversation_id}/messages", self.member_token
        ).json()
        ensure(
            messages[-1]["status"] == "redacted" and self.marker not in messages[-1]["content"],
            "REVOKED_ANSWER_VISIBLE",
        )
        run = self.request("GET", f"/runs/{self.run_id}", self.member_token).json()
        ensure(self.marker not in run["content"] and not run["references"], "REVOKED_RUN_VISIBLE")
        return {"history_redacted": True, "reference_denied": True, "run_redacted": True}

    def failed_rebuild(self):
        identifiers = self.request(
            "POST",
            f"/knowledge-bases/{self.kb_id}/documents",
            self.admin_token,
            expected=202,
            data={"document_id": self.doc_id},
            files={"file": ("empty.txt", b"   ", "text/plain")},
        ).json()
        failed = self.wait_task(identifiers["task_id"], expected="FAILED")
        ensure(failed.error_code == "EMPTY_TEXT", "FAILURE_NOT_DIAGNOSTIC")
        ensure(Document.objects.get(pk=self.doc_id).active_build_id == self.active_build_id, "OLD_BUILD_LOST")
        hits = self.request(
            "POST",
            "/retrieval/search",
            self.admin_token,
            json={"query": self.question, "kb_ids": [self.kb_id]},
        ).json()["sources"]
        ensure(any(h["build_id"] == str(self.active_build_id) for h in hits), "OLD_BUILD_NOT_RETRIEVABLE")
        return {"failed_task_id": str(failed.pk), "old_build_preserved": True}

    def outbox_recovery(self):
        # Only publication is fault-injected. Beat, Redis delivery, worker,
        # PostgreSQL and S3 must actually recover the pending database record.
        with patch(
            "apps.documents.tasks.process_document.delay", side_effect=OperationalError("verification loss")
        ) as publication:
            _, _, build, task = upload(
                RequestContext.for_user(self.admin),
                self.kb_id,
                SimpleUploadedFile("recovery.md", "补投演练资料：任务由数据库清单恢复。".encode()),
            )
            ensure(publication.call_count == 1, "PUBLISH_FAILURE_NOT_INJECTED")
        recovered = self.wait_task(task.pk)
        ensure(recovered.celery_task_id and recovered.attempt == 1, "RECOVERY_NOT_DELIVERED")
        ensure(build.chunks.count() > 0, "RECOVERED_BUILD_EMPTY")
        return {
            "task_id": str(task.pk),
            "attempt": recovered.attempt,
            "recovered_by_independent_scheduler": True,
        }

    def resource_accounting(self):
        models = self.request("GET", "/monitoring/models", self.admin_token).json()
        ensure(models["schema_matches"], "MODEL_SCHEMA_MISMATCH")
        self.request("GET", "/monitoring/models", self.member_token, expected=403)
        rows = UsageReservation.objects.filter(tenant=self.tenant)
        ensure(rows.exists() and not rows.filter(status="reserved").exists(), "USAGE_RESERVATIONS_LEAKED")
        self.request(
            "PUT", "/monitoring/quota", self.admin_token, json={"daily_tokens": 0, "user_daily_tokens": 0}
        )
        self.request(
            "POST",
            "/retrieval/search",
            self.admin_token,
            expected=429,
            json={"query": self.question, "kb_ids": [self.kb_id]},
        )
        return {
            "recorded_model_calls": rows.count(),
            "no_reservation_leak": True,
            "quota_denied_before_model": True,
            "model_schema_matches": True,
        }

    def controlled_agent(self):
        BusinessIssue.objects.create(
            tenant=self.tenant,
            occurred_at=timezone.now(),
            product="verification",
            category="service",
            description="synthetic agent sample",
        )
        questions = [
            ("search", self.question),
            ("summary", f"请总结文档 {self.doc_id}"),
            ("analysis", "统计2026-01-01至2026-12-31按类别分组的问题"),
        ]
        results = []
        for kind, question in questions:
            conversation = self.request(
                "POST", "/conversations", self.admin_token, expected=201, json={"kb_ids": [self.kb_id]}
            ).json()
            key = f"agent-{kind}-{self.suffix}"
            response = self.request(
                "POST",
                f"/conversations/{conversation['id']}/agent:stream",
                self.admin_token,
                headers={"Idempotency-Key": key},
                json={"question": question},
            )
            ensure("event: tool" in response.text and "event: done" in response.text, "AGENT_EVENTS_MISSING")
            run = Run.objects.get(conversation_id=conversation["id"])
            ensure(run.kind == "agent" and run.status == "completed", "AGENT_NOT_COMPLETED")
            ensure(
                run.toolcall_set.count() == 1 and run.toolcall_set.get().status == "completed",
                "AGENT_TOOL_NOT_AUDITED",
            )
            data = self.request("GET", f"/runs/{run.pk}", self.admin_token).json()
            ensure(data["references"] and "[S1]" in data["content"], "AGENT_CITATION_MISSING")
            ref = data["references"][0]
            route = "agents/references" if ref.get("kind") == "analysis" else "references"
            self.request("GET", f"/{route}/{ref['id']}", self.admin_token)
            self.request("GET", f"/{route}/{ref['id']}", self.outsider_token, expected=404)
            replay = self.request(
                "POST",
                f"/conversations/{conversation['id']}/agent:stream",
                self.admin_token,
                headers={"Idempotency-Key": key},
                json={"question": question},
            ).json()
            ensure(replay["replayed"], "AGENT_REPLAY_EXECUTED_TWICE")
            results.append(
                {
                    "kind": kind,
                    "run_id": str(run.pk),
                    "status": run.status,
                    "tool_calls": 1,
                    "citation_readable": True,
                }
            )
        return {
            "model": "demo_rules",
            "tools": results,
            "through_nginx": True,
            "worker_isolation": "one subprocess per tool",
        }

    def run(self):
        try:
            for name in [
                "dependencies",
                "database_permissions",
                "fixtures",
                "ingestion",
                "retrieval_and_stream",
                "controlled_agent",
                "revoked_history",
                "failed_rebuild",
                "outbox_recovery",
                "resource_accounting",
            ]:
                self.check(name, getattr(self, name))
            self.report["status"] = "passed"
        finally:
            if self.report["status"] == "running":
                self.report["status"] = "failed"
            self.report["finished_at"] = timezone.now().isoformat()
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.client.close()


class Command(BaseCommand):
    help = "Create verification fixtures and exercise a disposable PostgreSQL/Redis/MinIO deployment"

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://frontend")
        parser.add_argument("--report", default="/tmp/infrastructure-report.json")

    def handle(self, *args, **options):
        url = urlsplit(options["base_url"])
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.query or url.fragment:
            raise CommandError("Provide an HTTP(S) origin without credentials or query parameters")
        StackVerifier(options["base_url"], options["report"]).run()
