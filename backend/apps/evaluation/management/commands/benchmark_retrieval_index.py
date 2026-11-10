"""Synthetic index diagnostics against the real authorized product queryset.

Everything, including synthetic rows and DDL, rolls back. This is deliberately a
single-connection index diagnostic, not the 10-minute concurrent A10 load test.
"""

import hashlib
import json
import math
import random
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from ai_engine.retrieval import dense_queryset
from apps.documents.models import Document, DocumentChunk, DocumentVersion, IndexBuild
from apps.documents.selectors import active_chunks
from apps.evaluation.configuration import revision
from apps.evaluation.metrics import percentile
from apps.knowledge.models import KnowledgeBase, KnowledgeBaseACL
from apps.users.models import Tenant, User
from common.context import RequestContext


def random_vector(rng):
    values = [rng.gauss(0, 1) for _ in range(settings.EMBEDDING_DIMENSIONS)]
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values]


def used_indexes(plan):
    names = [plan["Index Name"]] if "Index Name" in plan else []
    for child in plan.get("Plans", []):
        names.extend(used_indexes(child))
    return names


def seed(size, rng):
    nonce = uuid.uuid4().hex[:12]
    own = Tenant.objects.create(name="S3 index diagnostic", slug=f"s3-index-{nonce}")
    other = Tenant.objects.create(name="S3 index foreign", slug=f"s3-other-{nonce}")
    admin = User.objects.create_user(f"admin-{nonce}@index.test", tenant=own, is_tenant_admin=True)
    member = User.objects.create_user(f"member-{nonce}@index.test", tenant=own)
    outsider = User.objects.create_user(f"other-{nonce}@index.test", tenant=other, is_tenant_admin=True)
    grantable, rows, sample = [], [], []
    for group in range(20):
        owner = admin if group < 10 else outsider
        kb = KnowledgeBase.objects.create(
            tenant=owner.tenant, owner=owner, name=f"index-scope-{group}", visibility="private"
        )
        if group < 10:
            grantable.append(kb)
        doc = Document.objects.create(tenant=owner.tenant, knowledge_base=kb, title=f"synthetic-{group}")
        version = DocumentVersion.objects.create(
            tenant=owner.tenant,
            document=doc,
            version_no=1,
            file=f"synthetic/{nonce}/{group}",
            original_name="synthetic.txt",
            sha256=hashlib.sha256(str(group).encode()).hexdigest(),
            mime_type="text/plain",
            size_bytes=1,
            created_by=owner,
        )
        build = IndexBuild.objects.create(
            tenant=owner.tenant,
            version=version,
            pipeline_hash="synthetic-index-v1",
            embedding_profile=settings.EMBEDDING_PROFILE,
            status="READY",
            config_snapshot={"synthetic": True},
        )
        Document.objects.filter(pk=doc.pk).update(active_build=build, desired_build=build)
        for n in range(size // 20):
            vector = random_vector(rng)
            if group == 0 and len(sample) < 32:
                sample.append(vector)
            text = f"synthetic {group} {n}"
            rows.append(
                DocumentChunk(
                    tenant=owner.tenant,
                    knowledge_base=kb,
                    document=doc,
                    build=build,
                    ordinal=n,
                    content=text,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    token_count=3,
                    embedding=vector,
                    search_tokens=text,
                    locator={"block": n, "char_start": 0, "char_end": len(text)},
                )
            )
    DocumentChunk.objects.bulk_create(rows, batch_size=200)
    return member, grantable, sample


class Command(BaseCommand):
    help = "Measure exact/HNSW on 1000/10000 synthetic chunks at 5%/50% authorization selectivity in a disposable PostgreSQL DB"

    def add_arguments(self, parser):
        parser.add_argument(
            "--disposable",
            action="store_true",
            help="Required: database is isolated and may briefly take index-build locks",
        )
        parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000])
        parser.add_argument("--queries", type=int, default=30)
        parser.add_argument("--ef-search", type=int, default=100)
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        if (
            not options["disposable"]
            or connection.vendor != "postgresql"
            or settings.EMBEDDING_DIMENSIONS > 2000
        ):
            raise CommandError(
                "An explicitly disposable PostgreSQL database with dimensions <=2000 is required"
            )
        if (
            not 1 <= options["queries"] <= 1000
            or not 5 <= options["ef_search"] <= 1000
            or any(s < 100 or s > 100000 or s % 20 for s in options["sizes"])
        ):
            raise CommandError("Invalid benchmark size/query/ef-search bounds")
        reports = []
        for size in options["sizes"]:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL jit = off")
                    cursor.execute("SET LOCAL statement_timeout = '120s'")
                member, bases, samples = seed(size, random.Random(20261110))
                with connection.cursor() as cursor:
                    tick = time.perf_counter()
                    cursor.execute(
                        "CREATE INDEX s3_diagnostic_hnsw ON documents_documentchunk USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)"
                    )
                    build_ms = (time.perf_counter() - tick) * 1000
                    cursor.execute("ANALYZE documents_documentchunk")
                    cursor.execute(
                        "SELECT pg_relation_size('s3_diagnostic_hnsw'), (SELECT extversion FROM pg_extension WHERE extname='vector'), version()"
                    )
                    index_bytes, vector_version, database_version = cursor.fetchone()
                for accessible in (1, 10):
                    KnowledgeBaseACL.objects.filter(user=member).delete()
                    KnowledgeBaseACL.objects.bulk_create(
                        [
                            KnowledgeBaseACL(
                                tenant=member.tenant, knowledge_base=kb, user=member, access_level="read"
                            )
                            for kb in bases[:accessible]
                        ]
                    )
                    ctx = RequestContext.for_user(member)
                    qs = active_chunks(ctx).filter(build__embedding_profile=settings.EMBEDDING_PROFILE)
                    records, plans = [], {}
                    for number in range(options["queries"] + 2):
                        vector = samples[number % len(samples)]
                        item = {"request": number - 2, "status": "success", "timing_ms": {}}
                        expected = None
                        for arm in ("exact", "hnsw"):
                            try:
                                # Nested savepoints retain failed requests without aborting
                                # the full report or dropping them from latency denominators.
                                with transaction.atomic():
                                    with connection.cursor() as cursor:
                                        cursor.execute(
                                            "SELECT set_config('enable_sort', %s, true)",
                                            ["on" if arm == "exact" else "off"],
                                        )
                                        cursor.execute(
                                            "SELECT set_config('enable_seqscan', %s, true)",
                                            ["on" if arm == "exact" else "off"],
                                        )
                                        cursor.execute(
                                            "SELECT set_config('enable_bitmapscan', %s, true)",
                                            ["on" if arm == "exact" else "off"],
                                        )
                                        cursor.execute(
                                            "SELECT set_config('hnsw.iterative_scan','strict_order',true)"
                                        )
                                        cursor.execute(
                                            "SELECT set_config('hnsw.ef_search',%s,true)",
                                            [str(options["ef_search"])],
                                        )
                                    query = dense_queryset(qs, vector, arm).values_list("pk", flat=True)[:5]
                                    tick = time.perf_counter()
                                    ids = list(query)
                                    item["timing_ms"][arm] = (time.perf_counter() - tick) * 1000
                                    if arm == "exact":
                                        expected = set(ids)
                                    else:
                                        item["recall_at_5_vs_exact"] = (
                                            len(set(ids) & expected) / len(expected) if expected else 0
                                        )
                                        item["returned"] = len(ids)
                                        item["unauthorized"] = len(ids) - qs.filter(pk__in=ids).count()
                                    if arm not in plans:
                                        plans[arm] = json.loads(
                                            query.explain(format="json", analyze=True, buffers=True)
                                        )[0]
                            except Exception:
                                item["status"] = "failed"
                                item["error"] = f"{arm.upper()}_QUERY_FAILED"
                                item["timing_ms"].setdefault(arm, (time.perf_counter() - tick) * 1000)
                                break
                        if number >= 2:
                            records.append(item)
                    recalls = [r.get("recall_at_5_vs_exact", 0) for r in records]
                    indexes = used_indexes(plans.get("hnsw", {}).get("Plan", {}))
                    reports.append(
                        {
                            "synthetic_chunks": size,
                            "tenants": 2,
                            "authorized_chunks": qs.count(),
                            "authorization_selectivity": accessible / 20,
                            "queries": options["queries"],
                            "warmup_per_arm": 2,
                            "index_build_ms": build_ms,
                            "index_bytes": index_bytes,
                            "pgvector": vector_version,
                            "postgresql": database_version,
                            "ef_search": options["ef_search"],
                            "iterative_scan": "strict_order",
                            "concurrency": 1,
                            "planner": {
                                "exact": "ordinary planner, arithmetic distance prevents ANN",
                                "hnsw": "diagnostic force: enable_sort/seqscan/bitmapscan=off; jit=off",
                            },
                            "hnsw_index_observed": "s3_diagnostic_hnsw" in indexes,
                            "recall_at_5_vs_exact": sum(recalls) / len(records),
                            "success_rate": sum(r["status"] == "success" for r in records) / len(records),
                            "unauthorized_occurrences": sum(r.get("unauthorized", 0) for r in records),
                            "exact_p95_ms": percentile([r["timing_ms"].get("exact", 0) for r in records]),
                            "hnsw_p95_ms": percentile([r["timing_ms"].get("hnsw", 0) for r in records]),
                            "plans": plans,
                            "requests": records,
                        }
                    )
                transaction.set_rollback(True)
        report = {
            "schema_version": 1,
            "code_revision": revision(),
            "vector_dimensions": settings.EMBEDDING_DIMENSIONS,
            "seed": 20261110,
            "data_kind": "synthetic_random_vectors_not_semantic_quality",
            "reports": reports,
            "acceptance_A10": "not_claimed: single-connection index diagnostic; not 1000 requests / 10 minutes / concurrency 10",
            "default_index_decision": "keep_exact_until_representative_quality_and_load_pass",
        }
        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.stdout.write(
            json.dumps(
                [
                    {k: v for k, v in r.items() if k not in {"plans", "requests", "postgresql", "planner"}}
                    for r in reports
                ]
            )
        )
        if any(
            r["success_rate"] != 1 or r["unauthorized_occurrences"] or not r["hnsw_index_observed"]
            for r in reports
        ):
            raise CommandError("Index diagnostic failed or did not observe HNSW; raw results retained")
