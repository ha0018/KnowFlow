"""Reproducible concurrent HTTP load over 10,000 synthetic published chunks."""

import hashlib
import json
import logging
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import httpx
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from rest_framework_simplejwt.tokens import RefreshToken

from ai_engine.embeddings import demo_embed, tokenize
from ai_engine.gateway.config import profile
from apps.documents.models import Document, DocumentChunk, DocumentVersion, IndexBuild, StorageObject
from apps.evaluation.configuration import environment
from apps.evaluation.metrics import percentile
from apps.knowledge.models import KnowledgeBase, KnowledgeBaseACL
from apps.users.models import Tenant, User


def seed():
    nonce = uuid.uuid4().hex[:12]
    corpus = hashlib.sha256()
    scopes = []
    with transaction.atomic():
        for tenant_number in range(2):
            tenant = Tenant.objects.create(
                name="S5 load",
                slug=f"s5-load-{nonce}-{tenant_number}",
                quota_config={"daily_tokens": 1000000000, "user_daily_tokens": 100000000},
            )
            owner = User.objects.create_user(
                f"owner-{nonce}-{tenant_number}@load.test", tenant=tenant, is_tenant_admin=True
            )
            bases, docs = [], []
            for group in range(10):
                kb = KnowledgeBase.objects.create(
                    tenant=tenant, owner=owner, name=f"Synthetic scope {group}", visibility="private"
                )
                texts = [f"产品 P{group:02}-{i:04} 退款政策：购买后30天内可以申请退款。" for i in range(500)]
                raw = "\n\n".join(texts).encode()
                corpus.update(raw)
                key = default_storage.save(f"tenants/{tenant.pk}/{uuid.uuid4().hex}.md", ContentFile(raw))
                StorageObject.objects.create(tenant=tenant, key=key, state="available")
                doc = Document.objects.create(
                    tenant=tenant, knowledge_base=kb, title=f"Synthetic load {group}"
                )
                version = DocumentVersion.objects.create(
                    tenant=tenant,
                    document=doc,
                    version_no=1,
                    file=key,
                    original_name=f"load-{group}.md",
                    sha256=hashlib.sha256(raw).hexdigest(),
                    mime_type="text/markdown",
                    size_bytes=len(raw),
                    created_by=owner,
                )
                blocks = [
                    {"text": text, "locator": {"source_char_start": sum(len(t) + 2 for t in texts[:i])}}
                    for i, text in enumerate(texts)
                ]
                build = IndexBuild.objects.create(
                    tenant=tenant,
                    version=version,
                    pipeline_hash="synthetic-load-v1",
                    config_snapshot={"synthetic": True, "chunks_per_document": 500},
                    embedding_profile=settings.EMBEDDING_PROFILE,
                    status="READY",
                    chunk_count=500,
                    parsed_blocks=blocks,
                )
                Document.objects.filter(pk=doc.pk).update(active_build=build, desired_build=build)
                rows = []
                for i, text in enumerate(texts):
                    rows.append(
                        DocumentChunk(
                            tenant=tenant,
                            knowledge_base=kb,
                            document=doc,
                            build=build,
                            ordinal=i,
                            content=text,
                            content_hash=hashlib.sha256(text.encode()).hexdigest(),
                            token_count=len(tokenize(text)),
                            embedding=demo_embed(text),
                            search_tokens=" ".join(tokenize(text)),
                            locator={
                                "block": i,
                                "char_start": 0,
                                "char_end": len(text),
                                **blocks[i]["locator"],
                            },
                        )
                    )
                DocumentChunk.objects.bulk_create(rows, batch_size=100)
                DocumentChunk.objects.filter(build=build).update(
                    search_vector=SearchVector("search_tokens", config="simple")
                )
                bases.append(kb)
                docs.append(str(doc.pk))
            scopes.append((tenant, bases, docs))
        clients = []
        for number in range(30):
            tenant, bases, docs = scopes[number % 2]
            visible = 1 if number % 4 < 2 else 10
            user = User.objects.create_user(f"reader-{nonce}-{number}@load.test", tenant=tenant)
            KnowledgeBaseACL.objects.bulk_create(
                [
                    KnowledgeBaseACL(tenant=tenant, knowledge_base=kb, user=user, access_level="read")
                    for kb in bases[:visible]
                ]
            )
            token = str(RefreshToken.for_user(user).access_token)
            clients.append(
                {
                    "group": "api" if number < 20 else "retrieval",
                    "actor": number,
                    "token": token,
                    "kb_ids": [str(kb.pk) for kb in bases[:visible]],
                    "doc_ids": docs[:visible],
                    "visible_chunks": visible * 500,
                }
            )
    return clients, corpus.hexdigest()


def request(client, actor, iteration):
    group = actor["group"]
    record = {
        "group": group,
        "actor": actor["actor"],
        "iteration": iteration,
        "visible_chunks": actor["visible_chunks"],
        "status": "failed",
    }
    started = time.perf_counter()
    try:
        headers = {"Authorization": "Bearer " + actor["token"]}
        if group == "api":
            if iteration % 2:
                kb_id = actor["kb_ids"][iteration % len(actor["kb_ids"])]
                record["endpoint"] = "documents"
                response = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents?page=1", headers=headers)
            else:
                record["endpoint"] = "knowledge_bases"
                response = client.get("/api/v1/knowledge-bases?page=1", headers=headers)
        else:
            response = client.post(
                "/api/v1/retrieval/search",
                headers=headers,
                json={
                    "query": f"产品 P00-{iteration % 500:04} 退款政策",
                    "top_k": 5,
                    "mode": "hybrid",
                    "rerank": False,
                    "rewrite": False,
                },
            )
        record["latency_ms"] = (time.perf_counter() - started) * 1000
        record["http_status"] = response.status_code
        record["trace_id"] = response.headers.get("x-trace-id")
        if response.status_code != 200:
            record["error_code"] = "HTTP_" + str(response.status_code)
            return record
        body = response.json()
        if group == "api":
            returned = {row["id"] for row in body["results"]}
            authorized = bool(returned) and returned <= set(
                actor["doc_ids"] if record["endpoint"] == "documents" else actor["kb_ids"]
            )
        else:
            sources = body["sources"]
            authorized = bool(sources) and all(row["document_id"] in actor["doc_ids"] for row in sources)
            record["returned"] = len(sources)
        if not authorized:
            record["error_code"] = "UNAUTHORIZED_OR_EMPTY_RESULT"
        else:
            record["status"] = "passed"
    except Exception as exc:
        record.update(error_code=type(exc).__name__, latency_ms=(time.perf_counter() - started) * 1000)
    return record


class Command(BaseCommand):
    help = "Run an explicitly disposable 20-user API/10-user hybrid HTTP benchmark; never use real model credentials"

    def add_arguments(self, parser):
        parser.add_argument("--disposable", action="store_true", required=True)
        parser.add_argument("--base-url", default="http://frontend")
        parser.add_argument("--duration", type=int, default=600)
        parser.add_argument("--min-requests", type=int, default=1000)
        parser.add_argument("--interval", type=float, default=0)
        parser.add_argument("--output", required=True)

    def handle(self, **options):
        if (
            connection.vendor != "postgresql"
            or profile("embedding").provider != "demo"
            or profile("generation").provider != "demo"
            or settings.RETRIEVAL_VECTOR_INDEX != "exact"
        ):
            raise CommandError("Disposable PostgreSQL and demo providers are required")
        if (
            not 1 <= options["duration"] <= 1800
            or not 1 <= options["min_requests"] <= 10000
            or not 0 <= options["interval"] <= 10
        ):
            raise CommandError("Invalid measurement bounds")
        actors, corpus_hash = seed()
        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        raw_path = output.with_suffix(".jsonl")
        records, warmup = [], []
        measurement_completed = False
        report = {
            "schema": "knowflow-load-v1",
            "revision": os.environ.get("CODE_REVISION", "workspace"),
            "environment": environment(),
            "scope": "demo_hash_embedding_10000_synthetic_published_chunks_2_tenants",
            "fixture_chunk_count": 10000,
            "fixture_schema": "synthetic-load-v1",
            "corpus_sha256": corpus_hash,
            "embedding_profile": settings.EMBEDDING_PROFILE,
            "database_chunk_count": DocumentChunk.objects.count(),
            "vector_index": "exact",
            "rerank": False,
            "rewrite": False,
            "concurrency": {"api": 20, "retrieval": 10},
            "visible_chunks": [500, 5000],
            "api_mix": "20 closed-loop users alternate knowledge-base and document list requests",
            "request_interval_seconds_per_user": options["interval"],
            "concurrent_workloads": True,
            "pagination_size": settings.REST_FRAMEWORK["PAGE_SIZE"],
            "real_supplier_verified": False,
            "api_user_rate": settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["user"],
            "fixture_daily_quota": {"tenant": 1000000000, "user": 100000000},
            "driver_location": "separate_container_on_same_host",
            "status": "running",
        }
        with (
            httpx.Client(
                base_url=options["base_url"],
                timeout=30,
                limits=httpx.Limits(max_connections=40, max_keepalive_connections=40),
            ) as client,
            ThreadPoolExecutor(max_workers=30) as pool,
            raw_path.open("w", encoding="utf-8") as raw,
        ):
            for iteration in range(2):
                futures = [pool.submit(request, client, actor, -2 + iteration) for actor in actors]
                warmup.extend(f.result() for f in futures)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            started = time.monotonic()
            counts = {"api": 0, "retrieval": 0}
            active, peak = {"api": 0, "retrieval": 0}, {"api": 0, "retrieval": 0}
            lock = Lock()

            def drive(actor):
                count, total = 0, 20 if actor["group"] == "api" else 10
                required = math.ceil(options["min_requests"] / total)
                while (
                    time.monotonic() - started < options["duration"] or count < required
                ) and time.monotonic() - started < options["duration"] + 300:
                    group = actor["group"]
                    with lock:
                        active[group] += 1
                        peak[group] = max(peak[group], active[group])
                    try:
                        record = request(client, actor, count)
                    finally:
                        with lock:
                            active[group] -= 1
                    with lock:
                        records.append(record)
                        counts[record["group"]] += 1
                        raw.write(json.dumps(record, sort_keys=True) + "\n")
                    count += 1
                    if options["interval"]:
                        time.sleep(options["interval"])

            try:
                futures = [pool.submit(drive, actor) for actor in actors]
                next_report = started + 30
                while not all(f.done() for f in futures):
                    if time.monotonic() >= next_report:
                        with lock:
                            raw.flush()
                            report.update(elapsed_seconds=time.monotonic() - started, counts=dict(counts))
                        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                        print(
                            json.dumps(
                                {"measurement_seconds": round(time.monotonic() - started), "counts": counts}
                            ),
                            flush=True,
                        )
                        next_report = time.monotonic() + 30
                    time.sleep(1)
                for future in futures:
                    future.result()
                raw.flush()
                measurement_completed = (
                    min(counts.values()) >= options["min_requests"]
                    and time.monotonic() - started >= options["duration"]
                )
            finally:
                report["elapsed_seconds"] = time.monotonic() - started
                report["peak_inflight"] = peak
                report["warmup"] = {
                    "requests": len(warmup),
                    "failed": sum(r["status"] != "passed" for r in warmup),
                }
                report["groups"] = {}
                for group, threshold in (("api", 300), ("retrieval", 800)):
                    items = [r for r in records if r["group"] == group]
                    latency = [r["latency_ms"] for r in items]
                    passed = sum(r["status"] == "passed" for r in items)
                    enough = (
                        measurement_completed
                        and options["interval"] == 0
                        and peak[group] == (20 if group == "api" else 10)
                        and len(items) >= 1000
                        and report["elapsed_seconds"] >= 600
                    )
                    p95 = percentile(latency)
                    report["groups"][group] = {
                        "requests": len(items),
                        "passed": passed,
                        "failed": len(items) - passed,
                        "success_rate": passed / len(items) if items else 0,
                        "p50_ms": percentile(latency, 0.5),
                        "p95_ms": p95,
                        "max_ms": max(latency) if latency else None,
                        "formal_measurement_length_met": enough,
                        "latency_threshold_ms": threshold,
                        "target_met": enough and p95 <= threshold and passed / len(items) >= 0.99,
                        "failures_in_latency_denominator": True,
                    }
                report["api_endpoints"] = {}
                for endpoint in ("knowledge_bases", "documents"):
                    items = [r for r in records if r.get("endpoint") == endpoint]
                    latency = [r["latency_ms"] for r in items]
                    passed = sum(r["status"] == "passed" for r in items)
                    p95 = percentile(latency)
                    enough = (
                        measurement_completed
                        and options["interval"] == 0
                        and peak["api"] == 20
                        and len(items) >= 1000
                        and report["elapsed_seconds"] >= 600
                    )
                    report["api_endpoints"][endpoint] = {
                        "requests": len(items),
                        "passed": passed,
                        "failed": len(items) - passed,
                        "p95_ms": p95,
                        "target_met": enough and p95 <= 300 and passed / len(items) >= 0.99,
                    }
                report["status"] = "completed" if measurement_completed else "interrupted"
                report["all_targets_met"] = (
                    all(g["target_met"] for g in report["groups"].values())
                    and all(g["target_met"] for g in report["api_endpoints"].values())
                    and report["warmup"]["failed"] == 0
                )
                report["raw_requests_file"] = raw_path.name
                raw.flush()
                with raw_path.open("rb") as source:
                    report["raw_requests_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
                output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "all_targets_met": report["all_targets_met"],
                    "groups": report["groups"],
                }
            ),
            flush=True,
        )
