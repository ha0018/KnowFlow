"""S5 disposable deployment acceptance. Every request/failure remains in the report."""

import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path

import httpx
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai_engine.gateway.config import profile
from apps.conversations.models import Conversation, Run
from apps.conversations.services import attach_sources, begin_run, save_progress
from apps.documents.models import Document, DocumentACL, DocumentTask
from apps.knowledge.models import KnowledgeBase, KnowledgeBaseACL
from apps.monitoring.delivery_fixtures import invalid_documents, valid_documents
from apps.monitoring.models import ModelCallLog
from apps.users.models import Permission, Role, Tenant, User, UserRole
from common.context import RequestContext
from common.models import AuditEvent


def ensure(condition, code):
    if not condition:
        raise AssertionError(code)


class DeliveryVerifier:
    def __init__(self, base_url, output):
        self.client = httpx.Client(base_url=base_url, timeout=70)
        self.output = Path(output)
        self.report = {
            "schema": "knowflow-delivery-v1",
            "revision": os.environ.get("CODE_REVISION", "workspace"),
            "started_at": timezone.now().isoformat(),
            "model": "demo",
            "status": "running",
            "checks": [],
        }
        self.nonce = uuid.uuid4().hex[:12]
        self.password = secrets.token_urlsafe(24)
        self.actors, self.tokens, self.documents = {}, {}, {}
        self.successful_tasks, self.completed_runs = [], []

    def request(self, actor, method, path, **kwargs):
        headers = {"Authorization": "Bearer " + self.tokens[actor]}
        headers.update(kwargs.pop("headers", {}))
        return self.client.request(method, "/api/v1" + path, headers=headers, **kwargs)

    def wait_task(self, task_id, seconds=240):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            task = DocumentTask.objects.select_related("build__version__document").get(pk=task_id)
            if task.status in {"READY", "FAILED", "CANCELLED"}:
                return task
            time.sleep(0.2)
        raise AssertionError("DOCUMENT_DEADLINE")

    def upload(self, name, raw, *, document_id=None, kb=None, actor="admin"):
        data = {"document_id": str(document_id)} if document_id else {}
        return self.request(
            actor,
            "POST",
            f"/knowledge-bases/{kb or self.kb.pk}/documents",
            files={"file": (name, raw, "application/octet-stream")},
            data=data,
        )

    def fixtures(self):
        self.tenant = Tenant.objects.create(name="S5 delivery", slug="s5-delivery-" + self.nonce)
        other = Tenant.objects.create(name="S5 foreign", slug="s5-foreign-" + self.nonce)
        for name in ("admin", "member", "editor", "foreign"):
            user = User.objects.create_user(
                f"{name}-{self.nonce}@delivery.test",
                self.password,
                tenant=other if name == "foreign" else self.tenant,
                is_tenant_admin=name in {"admin", "foreign"},
            )
            self.actors[name] = user
            if not user.is_tenant_admin:
                role, _ = Role.objects.get_or_create(
                    tenant=self.tenant, code="delivery-tools", defaults={"name": "验收工具角色"}
                )
                role.permissions.add(Permission.objects.get_or_create(code="agent.execute")[0])
                UserRole.objects.create(tenant=self.tenant, user=user, role=role)
            response = self.client.post(
                "/api/v1/auth/login", json={"email": user.email, "password": self.password}
            )
            ensure(response.status_code == 200, "LOGIN_FAILED")
            self.tokens[name] = response.json()["access"]
        self.kb = KnowledgeBase.objects.create(
            tenant=self.tenant, owner=self.actors["admin"], name="S5 acceptance", visibility="tenant"
        )
        for scope in ("public", "private", "restricted", "deleted", "foreign"):
            owner = "foreign" if scope == "foreign" else "admin"
            user = self.actors[owner]
            kb = self.kb
            if scope in {"private", "foreign"}:
                kb = KnowledgeBase.objects.create(
                    tenant=user.tenant, owner=user, name="S5 " + scope, visibility="private"
                )
            if scope == "private":
                KnowledgeBaseACL.objects.create(
                    tenant=self.tenant, knowledge_base=kb, user=self.actors["editor"], access_level="read"
                )
            marker = "s5-evidence-" + uuid.uuid4().hex
            response = self.upload(
                scope + ".md", (marker + " 退款须在30天内申请。").encode(), kb=kb.pk, actor=owner
            )
            ensure(response.status_code == 202, "FIXTURE_UPLOAD_FAILED")
            task = self.wait_task(response.json()["task_id"])
            ensure(task.status == "READY", "FIXTURE_PROCESSING_FAILED")
            doc = task.build.version.document
            if scope == "restricted":
                doc.acl_mode = "restricted"
                doc.save(update_fields=["acl_mode"])
                DocumentACL.objects.create(
                    tenant=self.tenant, document=doc, user=self.actors["editor"], access_level="read"
                )
            ctx = RequestContext.for_user(user)
            conversation = Conversation.objects.create(tenant=user.tenant, user=user)
            run, _ = begin_run(ctx, conversation.pk, "fixture reference", "s5-ref-" + uuid.uuid4().hex)
            chunk = task.build.chunks.first()
            source = {
                "chunk_id": str(chunk.pk),
                "source_id": "S1",
                "content": chunk.content,
                "score": 1.0,
                "locator": chunk.locator,
            }
            attach_sources(ctx, run, [source])
            save_progress(run.pk, marker + " [S1]", "completed", {"source": "unknown"})
            if scope == "deleted":
                Document.objects.filter(pk=doc.pk).update(deleted_at=timezone.now())
            self.documents[scope] = {
                "document": doc,
                "task": task,
                "marker": marker,
                "owner": owner,
                "run": run,
                "reference": run.output_message.references.get(),
            }
        return {"tenants": 2, "actors": 4, "resource_scopes": list(self.documents)}

    def allowed(self, actor, scope):
        return scope != "deleted" and (
            actor == "foreign"
            if scope == "foreign"
            else actor != "foreign" and (scope == "public" or actor == "admin" or actor == "editor")
        )

    def security(self):
        records = []
        self.report["checks"][-1]["details"] = {"cases": records}
        for actor in self.actors:
            for scope, item in self.documents.items():
                allowed = self.allowed(actor, scope)
                owned = actor == item["owner"]
                doc, task, run, ref = (item[key] for key in ("document", "task", "run", "reference"))
                paths = {
                    "document": ("GET", f"/documents/{doc.pk}", {}),
                    "task": ("GET", f"/tasks/{task.pk}", {}),
                    "download": ("GET", f"/documents/{doc.pk}/download", {}),
                    "search": ("POST", "/retrieval/search", {"json": {"query": item["marker"], "top_k": 8}}),
                    "tool": (
                        "POST",
                        "/agents/tools",
                        {"json": {"tool": "DocumentSummaryTool", "args": {"document_id": str(doc.pk)}}},
                    ),
                    "reference": ("GET", f"/references/{ref.pk}", {}),
                    "history": ("GET", f"/conversations/{run.conversation_id}/messages", {}),
                    "run": ("GET", f"/runs/{run.pk}", {}),
                }
                for surface, (method, path, kwargs) in paths.items():
                    record = {
                        "actor": actor,
                        "scope": scope,
                        "surface": surface,
                        "read_allowed": allowed,
                        "status": "failed",
                    }
                    records.append(record)
                    response = self.request(actor, method, path, **kwargs)
                    record["http_status"] = response.status_code
                    expected = (
                        200
                        if (
                            allowed
                            if surface in {"document", "task", "download", "tool"}
                            else owned and allowed
                            if surface == "reference"
                            else owned
                            if surface in {"history", "run"}
                            else True
                        )
                        else 404
                    )
                    ensure(response.status_code == expected, "SECURITY_STATUS_MISMATCH")
                    if not allowed or (surface in {"reference", "history", "run"} and not owned):
                        returned_evidence = (
                            json.dumps(response.json()["sources"]).encode()
                            if surface == "search"
                            else response.content
                        )
                        ensure(item["marker"].encode() not in returned_evidence, "UNAUTHORIZED_ORIGINAL_TEXT")
                    if surface == "search":
                        ids = {source["chunk_id"] for source in response.json()["sources"]}
                        if not allowed:
                            ensure(str(ref.chunk_id) not in ids, "UNAUTHORIZED_CHUNK")
                    record["status"] = "passed"
        return {
            "case_count": len(records),
            "failed": sum(r["status"] != "passed" for r in records),
            "unauthorized_occurrences": 0,
            "cases": records,
        }

    def ingestion(self):
        submitted, valid, invalid = [], [], []
        self.report["checks"][-1]["details"] = {"valid": valid, "invalid": invalid}
        for name, raw in valid_documents():
            record = {
                "name": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "status": "failed",
            }
            valid.append(record)
            response = self.upload(name, raw)
            ensure(response.status_code == 202, "VALID_UPLOAD_REJECTED")
            record["task_id"] = response.json()["task_id"]
            submitted.append(record)
        for record in submitted:
            task = self.wait_task(record["task_id"])
            record.update(
                status="passed" if task.status == "READY" else "failed",
                error_code=task.error_code,
                chunks=task.build.chunks.count(),
            )
            self.successful_tasks.append(task)
        for name, raw, expected in invalid_documents():
            record = {
                "name": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "expected": expected,
                "status": "failed",
            }
            invalid.append(record)
            response = self.upload(name, raw)
            if response.status_code == 202:
                task = self.wait_task(response.json()["task_id"])
                record.update(task_id=str(task.pk), error_code=task.error_code)
                ensure(
                    task.status == "FAILED" and task.error_code == expected, "INVALID_DOCUMENT_STATE_MISMATCH"
                )
            else:
                ensure(
                    response.status_code == 400 and expected in response.text, "INVALID_UPLOAD_ERROR_MISMATCH"
                )
            record["status"] = "passed"
        ensure(sum(r["status"] == "passed" for r in valid) >= 48, "VALID_INGESTION_THRESHOLD")
        self.valid_records, self.invalid_records = valid, invalid
        return {
            "valid_total": 50,
            "valid_passed": sum(r["status"] == "passed" for r in valid),
            "invalid_total": 20,
            "invalid_passed": len(invalid),
            "valid": valid,
            "invalid": invalid,
        }

    def duplicates(self):
        from apps.documents.tasks import process_document

        task = self.successful_tasks[0]
        before = list(task.build.chunks.values_list("id", "content_hash"))
        records = []
        for i in range(10):
            queued = process_document.delay(str(task.pk))
            records.append({"delivery": i + 1, "queue_id": queued.id, "status": "queued"})
        response = self.upload(
            "duplicate-queue-barrier.md", b"A queue barrier after the duplicate deliveries."
        )
        ensure(response.status_code == 202, "QUEUE_BARRIER_UPLOAD_FAILED")
        barrier = self.wait_task(response.json()["task_id"])
        ensure(barrier.status == "READY", "QUEUE_BARRIER_FAILED")
        ensure(list(task.build.chunks.values_list("id", "content_hash")) == before, "DUPLICATE_CHUNKS")
        for record in records:
            record["status"] = "passed"
        return {
            "deliveries": 10,
            "scope": "ten actual Redis/Celery redeliveries followed by a processing barrier; worker interruption is recorded separately",
            "barrier_task_id": str(barrier.pk),
            "cases": records,
        }

    def versions(self):
        item = self.documents["public"]
        doc = item["document"]
        active = Document.objects.get(pk=doc.pk).active_build_id
        records = []
        for i in range(10):
            response = self.upload(f"failed-update-{i}.md", b" \n ", document_id=doc.pk)
            ensure(response.status_code == 202, "UPDATE_REJECTED")
            task = self.wait_task(response.json()["task_id"])
            ensure(
                task.status == "FAILED" and Document.objects.get(pk=doc.pk).active_build_id == active,
                "OLD_BUILD_LOST",
            )
            reference = self.request("admin", "GET", f"/references/{item['reference'].pk}")
            ensure(reference.status_code == 200 and item["marker"] in reference.text, "OLD_REFERENCE_LOST")
            records.append({"iteration": i + 1, "task_id": str(task.pk), "status": "passed"})
        from unittest.mock import patch

        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.documents.services import upload
        from apps.documents.tasks import process_document

        stale = []
        for i in range(5):
            ctx = RequestContext.for_user(self.actors["admin"])
            with patch("apps.documents.services.dispatch"):
                _, _, old, old_task = upload(
                    ctx,
                    self.kb.pk,
                    SimpleUploadedFile(f"late-{i}.md", b"Superseded policy."),
                    document_id=doc.pk,
                )
                _, _, new, new_task = upload(
                    ctx,
                    self.kb.pk,
                    SimpleUploadedFile(f"latest-{i}.md", b"Latest authorized policy."),
                    document_id=doc.pk,
                )
            process_document.delay(str(new_task.pk))
            ensure(self.wait_task(new_task.pk).status == "READY", "LATEST_BUILD_FAILED")
            process_document.delay(str(old_task.pk))
            ensure(self.wait_task(old_task.pk).status == "CANCELLED", "LATE_BUILD_NOT_CANCELLED")
            ensure(Document.objects.get(pk=doc.pk).active_build_id == new.pk, "LATE_BUILD_OVERWROTE_LATEST")
            stale.append(
                {"iteration": i + 1, "old_build": str(old.pk), "new_build": str(new.pk), "status": "passed"}
            )
        return {
            "failed_updates": 10,
            "old_active_preserved": True,
            "old_reference_readable": True,
            "cases": records,
            "superseded_build_cases": stale,
        }

    def stream_cycles(self):
        records = []
        self.report["checks"][-1]["details"] = {"cases": records}
        for number in range(20):
            mode = "cancel" if number % 2 else "disconnect"
            conversation = self.request("admin", "POST", "/conversations", json={"kb_ids": [str(self.kb.pk)]})
            ensure(conversation.status_code == 201, "STREAM_CONVERSATION_FAILED")
            run_id = None
            with self.client.stream(
                "POST",
                f"/api/v1/conversations/{conversation.json()['id']}/messages:stream",
                headers={
                    "Authorization": "Bearer " + self.tokens["admin"],
                    "Idempotency-Key": "s5-stream-" + uuid.uuid4().hex,
                },
                json={"question": "请给出退款期限的原文依据。"},
            ) as response:
                ensure(response.status_code == 200, "STREAM_REJECTED")
                current = ""
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        current = line[6:].strip()
                    if current == "meta" and line.startswith("data:"):
                        run_id = json.loads(line[5:])["run_id"]
                        if mode == "cancel":
                            ensure(
                                self.request("admin", "POST", f"/runs/{run_id}/cancel").status_code == 200,
                                "CANCEL_REJECTED",
                            )
                        break
            ensure(run_id is not None, "STREAM_META_MISSING")
            deadline = time.monotonic() + 15
            run = Run.objects.select_related("output_message").get(pk=run_id)
            while run.status == "streaming" and time.monotonic() < deadline:
                time.sleep(0.1)
                run = Run.objects.select_related("output_message").get(pk=run_id)
            ensure(
                run.status != "streaming" and run.output_message.status == run.status,
                "STREAM_TERMINAL_MISMATCH",
            )
            records.append(
                {
                    "iteration": number + 1,
                    "mode": mode,
                    "run_id": run_id,
                    "status": "passed",
                    "terminal": run.status,
                    "saved_content_sha256": hashlib.sha256(run.output_message.content.encode()).hexdigest(),
                }
            )
        return {
            "case_count": len(records),
            "pending_streaming": 0,
            "cases": records,
            "timeout_note": "Native HTTP timeout and partial-content cancellation are additionally covered by protocol JUnit tests.",
        }

    def normal_runs(self):
        for i in range(10):
            conversation = self.request("admin", "POST", "/conversations", json={"kb_ids": [str(self.kb.pk)]})
            ensure(conversation.status_code == 201, "CONVERSATION_FAILED")
            response = self.request(
                "admin",
                "POST",
                f"/conversations/{conversation.json()['id']}/messages:stream",
                json={"question": "退款期限是多少？"},
                headers={"Idempotency-Key": "s5-observe-" + uuid.uuid4().hex},
            )
            ensure(response.status_code == 200, "RAG_FAILED")
            meta = next(b for b in response.text.split("\n\n") if b.startswith("event: meta"))
            run_id = json.loads(meta.split("data: ", 1)[1])["run_id"]
            run = Run.objects.get(pk=run_id)
            ensure(run.status == "completed", "RAG_NOT_COMPLETED")
            self.completed_runs.append(run)
        return {"completed": len(self.completed_runs)}

    def traceability(self):
        samples = []
        for task in self.successful_tasks[:10]:
            ensure(bool(task.trace_id), "TASK_TRACE_MISSING")
            ensure(
                ModelCallLog.objects.filter(task=task, trace_id=task.trace_id).exists(),
                "TASK_MODEL_TRACE_MISSING",
            )
            ensure(
                AuditEvent.objects.filter(
                    action="document.publish",
                    resource_id=str(task.build.version.document_id),
                    trace_id=task.trace_id,
                ).exists(),
                "PUBLISH_TRACE_MISSING",
            )
            samples.append(
                {"kind": "task", "id": str(task.pk), "trace_id": task.trace_id, "status": "passed"}
            )
        for run in self.completed_runs:
            ensure(
                ModelCallLog.objects.filter(run=run, trace_id=run.trace_id).exists(),
                "RUN_MODEL_TRACE_MISSING",
            )
            ensure(run.usage.get("source") == "unknown", "DEMO_USAGE_NOT_UNKNOWN")
            samples.append(
                {
                    "kind": "run",
                    "id": str(run.pk),
                    "trace_id": run.trace_id,
                    "usage_source": run.usage.get("source"),
                    "status": "passed",
                }
            )
        return {"sample_count": len(samples), "cases": samples}

    def run(self):
        try:
            for name in (
                "fixtures",
                "security",
                "ingestion",
                "duplicates",
                "versions",
                "stream_cycles",
                "normal_runs",
                "traceability",
            ):
                started = time.monotonic()
                item = {"name": name, "status": "running"}
                self.report["checks"].append(item)
                self.output.parent.mkdir(parents=True, exist_ok=True)
                self.output.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
                try:
                    item["details"] = getattr(self, name)()
                    item["status"] = "passed"
                    print("PASS " + name, flush=True)
                except Exception as exc:
                    item.update(
                        status="failed",
                        error=str(exc) if isinstance(exc, AssertionError) else type(exc).__name__,
                    )
                    raise
                finally:
                    item["duration_ms"] = round((time.monotonic() - started) * 1000)
            self.report["status"] = "passed"
        finally:
            self.report["finished_at"] = timezone.now().isoformat()
            if self.report["status"] != "passed":
                self.report["status"] = "failed"
            self.output.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.client.close()


class Command(BaseCommand):
    help = "Run S5 acceptance against a disposable deployed demo stack"

    def add_arguments(self, parser):
        parser.add_argument("--disposable", action="store_true", required=True)
        parser.add_argument("--base-url", default="http://frontend")
        parser.add_argument("--output", required=True)

    def handle(self, **options):
        if profile("generation").provider != "demo" or profile("embedding").provider != "demo":
            raise CommandError("Only disposable demo providers are supported")
        DeliveryVerifier(options["base_url"], options["output"]).run()
