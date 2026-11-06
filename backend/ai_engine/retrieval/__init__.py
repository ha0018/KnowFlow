from collections import defaultdict

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db import connection
from pgvector.django import CosineDistance

from ai_engine.embeddings import tokenize
from ai_engine.gateway import embed
from apps.documents.selectors import active_chunks


def rrf(rankings, constant=60):
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, key in enumerate(ranking, 1):
            scores[key] += 1 / (constant + rank)
    return sorted(scores, key=lambda key: (-scores[key], str(key))), scores


def search(ctx, query, kb_ids=None, top_k=5, mode="hybrid"):
    vector, _ = embed([query])
    vector = vector[0]
    terms = set(tokenize(query))
    qs = active_chunks(ctx, kb_ids).filter(build__embedding_profile=settings.EMBEDDING_PROFILE)
    qs = qs.select_related("document", "build__version")
    if connection.vendor == "postgresql":
        dense = list(
            qs.annotate(distance=CosineDistance("embedding", vector)).order_by("distance", "id")[:40]
        )
        tsquery = (
            SearchQuery(" | ".join(sorted(terms)), config="simple", search_type="raw") if terms else None
        )
        keyword = (
            list(
                qs.filter(search_vector=tsquery)
                .annotate(rank=SearchRank("search_vector", tsquery))
                .order_by("-rank", "id")[:40]
            )
            if tsquery
            else []
        )
        rows = {row.pk: row for row in dense + keyword}
    else:
        # Portable local baseline; fail explicitly instead of silently missing later documents.
        if qs.count() > 5000:
            raise ValueError("LOCAL_CORPUS_LIMIT_USE_POSTGRESQL")
        candidates = list(qs)
        dense = sorted(
            candidates, key=lambda c: (-sum(a * b for a, b in zip(c.embedding, vector)), str(c.pk))
        )[:40]
        keyword = sorted(
            [c for c in candidates if terms & set(c.search_tokens.split())],
            key=lambda c: (-len(terms & set(c.search_tokens.split())), str(c.pk)),
        )[:40]
        rows = {row.pk: row for row in dense + keyword}
    if settings.EMBEDDING_PROVIDER == "demo":
        # Hash vectors are only a mechanics demo; require literal term evidence to avoid random matches.
        dense = [row for row in dense if terms & set(row.search_tokens.split())]
    rankings = [[c.pk for c in dense]]
    if mode == "hybrid":
        rankings.append([c.pk for c in keyword])
    ids, scores = rrf(rankings)
    result = []
    counts = defaultdict(int)
    for key in ids:
        row = rows[key]
        if counts[row.document_id] >= 2:
            continue
        counts[row.document_id] += 1
        result.append(
            {
                "chunk_id": str(row.pk),
                "document_id": str(row.document_id),
                "build_id": str(row.build_id),
                "title": row.document.title,
                "version": row.build.version.version_no,
                "content": row.content,
                "locator": row.locator,
                "score": scores[key],
                "source_id": f"S{len(result) + 1}",
            }
        )
        if len(result) >= top_k:
            break
    return result
