import math
import time
from collections import defaultdict
from dataclasses import replace

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db import connection, transaction
from django.db.models import F, FloatField, Value
from pgvector.django import CosineDistance

from ai_engine.embeddings import tokenize
from ai_engine.gateway import embed
from ai_engine.gateway.config import GatewayError
from apps.documents.selectors import active_chunks
from apps.users.models import Tenant, User

from .adapters import rerank, rewrite_query
from .options import defaults


def rrf(rankings, constant=60):
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, key in enumerate(ranking, 1):
            scores[key] += 1 / (constant + rank)
    return sorted(scores, key=lambda key: (-scores[key], str(key))), scores


def dense_queryset(qs, vector, index="exact"):
    qs = qs.annotate(distance=CosineDistance("embedding", vector))
    # Arithmetic prevents an approximate index from changing the exact baseline.
    if index == "exact":
        return qs.order_by(F("distance") + Value(0.0, output_field=FloatField()), "id")
    return qs.order_by("distance")


def dense_rows(qs, vector, options, trace):
    if connection.vendor != "postgresql":
        if options.vector_index != "exact":
            raise GatewayError("HNSW_REQUIRES_POSTGRESQL")
        if qs.count() > 5000:
            raise ValueError("LOCAL_CORPUS_LIMIT_USE_POSTGRESQL")
        return sorted(
            qs,
            key=lambda row: (
                -math.fsum(float(a) * float(b) for a, b in zip(row.embedding, vector))
                / (math.hypot(*map(float, row.embedding)) or 1),
                str(row.pk),
            ),
        )[: options.candidate_k]
    if options.vector_index == "exact":
        return list(dense_queryset(qs, vector)[: options.candidate_k])
    if settings.EMBEDDING_DIMENSIONS > 2000:
        raise GatewayError("HNSW_VECTOR_DIMENSIONS_UNSUPPORTED")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)")
            cursor.execute("SELECT set_config('hnsw.ef_search', %s, true)", [str(options.ef_search)])
        rows = list(dense_queryset(qs, vector, "hnsw")[: options.candidate_k])
    if len(rows) < options.candidate_k:
        trace["ann_fallback"] = "filtered_underfill"
        rows = list(dense_queryset(qs, vector)[: options.candidate_k])
    return sorted(rows, key=lambda row: (row.distance, str(row.pk)))


def search(
    ctx, query, kb_ids=None, top_k=5, mode=None, *, run=None, options=None, history=(), diagnostics=None
):
    options = options or defaults()
    if mode is not None:
        options = replace(options, mode=mode)
    options.validate()
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 4000
        or type(top_k) is not int
        or not 1 <= top_k <= 8
    ):
        raise ValueError("RETRIEVAL_INPUT_INVALID")
    trace = diagnostics if diagnostics is not None else {}
    trace.update(config=options.snapshot(), stages_ms={}, candidates=[], ann_fallback=None)
    started = time.perf_counter()
    epoch = Tenant.objects.get(pk=ctx.tenant_id).acl_epoch
    captured_ids = []

    def guard():
        if (
            not User.objects.filter(
                pk=ctx.user.pk, tenant_id=ctx.tenant_id, is_active=True, tenant__is_active=True
            ).exists()
            or not Tenant.objects.filter(pk=ctx.tenant_id, acl_epoch=epoch).exists()
            or (
                captured_ids
                and active_chunks(ctx, kb_ids).filter(pk__in=captured_ids).count() != len(captured_ids)
            )
        ):
            raise GatewayError("PERMISSION_CHANGED")
        if run is not None:
            from apps.conversations.services import run_is_allowed

            if not run_is_allowed(ctx, run.pk):
                raise GatewayError("PERMISSION_CHANGED")

    try:
        guard()
        qs = (
            active_chunks(ctx, kb_ids)
            .filter(build__embedding_profile=settings.EMBEDDING_PROFILE)
            .select_related("document", "build__version")
        )
        if not qs.exists():
            return []
        tick = time.perf_counter()
        rewritten = rewrite_query(ctx, query, history, run=run, guard=guard) if options.rewrite else query
        trace["rewritten_query"] = rewritten
        trace["stages_ms"]["rewrite"] = (time.perf_counter() - tick) * 1000
        tick = time.perf_counter()
        vectors, _ = embed([rewritten], ctx=ctx, run=run, before_request=guard)
        trace["stages_ms"]["embedding"] = (time.perf_counter() - tick) * 1000
        tick = time.perf_counter()
        dense = dense_rows(qs, vectors[0], options, trace)
        terms = set(tokenize(rewritten))
        trace["stages_ms"]["dense"] = (time.perf_counter() - tick) * 1000
        tick = time.perf_counter()
        keyword = []
        if options.mode == "hybrid":
            if connection.vendor == "postgresql":
                lexemes = ["'" + term.replace("'", "''") + "'" for term in sorted(terms)]
                tsquery = (
                    SearchQuery(" | ".join(lexemes), config="simple", search_type="raw") if lexemes else None
                )
                keyword = (
                    list(
                        qs.filter(search_vector=tsquery)
                        .annotate(rank=SearchRank("search_vector", tsquery))
                        .order_by("-rank", "id")[: options.candidate_k]
                    )
                    if tsquery
                    else []
                )
            else:
                keyword = sorted(
                    [row for row in qs if terms & set(row.search_tokens.split())],
                    key=lambda row: (-len(terms & set(row.search_tokens.split())), str(row.pk)),
                )[: options.candidate_k]
        if settings.EMBEDDING_PROVIDER == "demo":
            dense = [row for row in dense if terms & set(row.search_tokens.split())]
        trace["stages_ms"]["keyword"] = (time.perf_counter() - tick) * 1000
        tick = time.perf_counter()
        rows = {row.pk: row for row in dense + keyword}
        captured_ids = list(rows)
        guard()
        rankings = [[row.pk for row in dense]]
        if options.mode == "hybrid":
            rankings.append([row.pk for row in keyword])
        ids, scores = rrf(rankings, options.rrf_constant)
        trace["stages_ms"]["fusion"] = (time.perf_counter() - tick) * 1000
        tick = time.perf_counter()
        rerank_scores = {}
        if options.rerank:
            reranked, rerank_scores = rerank(
                ctx, rewritten, [rows[key] for key in ids[: options.rerank_k]], run=run, guard=guard
            )
            ids = [row.pk for row in reranked] + ids[options.rerank_k :]
        trace["stages_ms"]["rerank"] = (time.perf_counter() - tick) * 1000
        guard()
        dense_ranks = {row.pk: n for n, row in enumerate(dense, 1)}
        keyword_ranks = {row.pk: n for n, row in enumerate(keyword, 1)}
        trace["candidates"] = [
            {
                "chunk_id": str(key),
                "dense_rank": dense_ranks.get(key),
                "keyword_rank": keyword_ranks.get(key),
                "rrf": scores[key],
                "rerank": rerank_scores.get(key),
            }
            for key in ids
        ]
        result, counts = [], defaultdict(int)
        for key in ids:
            row = rows[key]
            if counts[row.document_id] >= options.per_document:
                continue
            counts[row.document_id] += 1
            result.append(
                {
                    "chunk_id": str(row.pk),
                    "document_id": str(row.document_id),
                    "build_id": str(row.build_id),
                    "version_id": str(row.build.version_id),
                    "sha256": row.build.version.sha256,
                    "title": row.document.title,
                    "version": row.build.version.version_no,
                    "content": row.content,
                    "locator": row.locator,
                    "score": scores[key],
                    "rerank_score": rerank_scores.get(key),
                    "source_id": f"S{len(result) + 1}",
                }
            )
            if len(result) == top_k:
                break
        return result
    finally:
        trace["stages_ms"]["total"] = (time.perf_counter() - started) * 1000
