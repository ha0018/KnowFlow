"""Only fixed, authorized read operations. Called inside the killable worker."""

from django.db.models import Count
from django.db.models.functions import TruncMonth

from ai_engine.embeddings import tokenize
from ai_engine.retrieval import search
from ai_engine.retrieval.options import RetrievalOptions, defaults
from apps.documents.selectors import active_chunks, get_document
from apps.knowledge.selectors import knowledge_bases

from .models import BusinessIssue
from .policy import fresh_context
from .schemas import validate_args


def execute_core(ctx, name, args, run=None):
    ctx = fresh_context(ctx, run)
    params = validate_args(name, args)
    scope = run.config_snapshot.get("kb_ids") or None if run else None
    if name == "KnowledgeSearchTool":
        ids = params.get("kb_ids")
        if ids is not None and knowledge_bases(ctx).filter(pk__in=ids).count() != len(ids):
            from django.http import Http404

            raise Http404()
        if scope and ids and not set(map(str, ids)) <= set(scope):
            from ai_engine.gateway.config import GatewayError

            raise GatewayError("TOOL_SCOPE_REJECTED")
        value = run.config_snapshot.get("retrieval_options", {}) if run else {}
        options = (
            RetrievalOptions(**{k: v for k, v in value.items() if k in RetrievalOptions.__dataclass_fields__})
            if run
            else defaults()
        )
        result = {
            "sources": search(ctx, params["query"], ids or scope, params["top_k"], run=run, options=options)
        }
    elif name == "DocumentSummaryTool":
        doc = get_document(ctx, params["document_id"])
        if scope and str(doc.knowledge_base_id) not in scope:
            from ai_engine.gateway.config import GatewayError

            raise GatewayError("TOOL_SCOPE_REJECTED")
        chunks = list(
            active_chunks(ctx)
            .filter(document=doc)
            .select_related("build__version", "document")
            .order_by("ordinal")[:80]
        )
        if params["focus"]:
            terms = set(tokenize(params["focus"]))
            chunks.sort(key=lambda c: (-len(terms & set(c.search_tokens.split())), c.ordinal))
        result = {
            "title": doc.title,
            "mode": "extractive",
            "truncated": bool(doc.active_build and doc.active_build.chunk_count > 8),
            "inspected_chunks": len(chunks),
            "version": doc.active_build.version.version_no if doc.active_build else None,
            "sources": [
                {
                    "chunk_id": str(c.pk),
                    "document_id": str(c.document_id),
                    "build_id": str(c.build_id),
                    "version_id": str(c.build.version_id),
                    "title": doc.title,
                    "version": c.build.version.version_no,
                    "content": c.content[:500],
                    "text": c.content[:500],
                    "locator": c.locator,
                    "score": 0.0,
                }
                for c in chunks[:8]
            ],
        }
    else:
        rows = BusinessIssue.objects.filter(
            tenant_id=ctx.tenant_id,
            occurred_at__date__gte=params["date_from"],
            occurred_at__date__lte=params["date_to"],
        )
        if params.get("product"):
            rows = rows.filter(product=params["product"])
        group = params["group_by"]
        if group == "month":
            rows = rows.annotate(month=TruncMonth("occurred_at"))
        groups = list(rows.order_by().values(group).annotate(count=Count("id")).order_by(group)[:51])
        result = {
            "groups": groups[:50],
            "groups_truncated": len(groups) > 50,
            "total": rows.count(),
            "group_by": group,
            "product": params.get("product"),
            "date_from": str(params["date_from"]),
            "date_to": str(params["date_to"]),
            "scope": "当前租户演示问题；按 occurred_at UTC 日期（含首尾）计数；分组最多50项，total为全范围计数",
        }
    fresh_context(ctx, run, result.get("sources", ()))
    return result
