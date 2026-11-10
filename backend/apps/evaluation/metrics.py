import math
import re


def percentile(values, quantile=0.95):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def covers(hit, gold):
    locator = hit.get("locator", {})
    return (
        hit.get("document_id") == gold["document_id"]
        and hit.get("version_id") == gold.get("version_id")
        and hit.get("sha256") == gold.get("sha256")
        and locator.get("block") == gold.get("block")
        and type(gold.get("char_start")) is int
        and locator.get("char_start", float("inf")) <= gold["char_start"]
        and locator.get("char_end", -1) >= gold["char_end"]
    )


def retrieval_metrics(hits, gold):
    hits = hits[:5]
    versions = {(h["document_id"], h.get("version_id")) for h in hits}
    expected = {(g["document_id"], g.get("version_id")) for g in gold}
    annotated = bool(gold) and all("block" in g and "char_end" in g for g in gold)
    relevance = [any(covers(hit, g) for g in gold) for hit in hits] if annotated else None
    return {
        "doc_recall_at_5": len(versions & expected) / len(expected) if expected else None,
        "evidence_hit_at_5": float(all(any(covers(hit, g) for hit in hits) for g in gold))
        if annotated
        else None,
        "mrr_at_5": next((1 / rank for rank, related in enumerate(relevance, 1) if related), 0.0)
        if annotated
        else None,
        "precision_at_5": sum(relevance) / 5 if annotated else None,
        "evidence_annotations_complete": annotated,
    }


def citation_metrics(answer, sources):
    cited = re.findall(r"\[(S\d+)\]", answer)
    ids = {s["source_id"] for s in sources if s.get("locator")}
    return {
        "citations_total": len(cited),
        "citations_valid": sum(c in ids for c in cited),
        "citation_validity": sum(c in ids for c in cited) / len(cited) if cited else None,
    }


def aggregate(results, cases, *, provider):
    by_case = {str(r.case_id): r for r in results}
    answerable = [c for c in cases if c.category == "answerable"]
    errors = sum(bool(r.error) for r in results)
    output = {
        "case_count": len(cases),
        "answerable_evaluated": len(answerable),
        "errors": errors,
        "provider": provider,
        "quality_scope": "demo_mechanics" if provider == "demo" else "configured_model",
        "note": "失败题保留在分母；回答支撑与事实正确性仅来自人工复核。",
    }
    for name in ("doc_recall_at_5", "evidence_hit_at_5", "mrr_at_5", "precision_at_5"):
        values = []
        for case in answerable:
            row = by_case.get(str(case.pk))
            value = row.metrics.get(name) if row and not row.error else None
            if value is not None or (row and row.error):
                values.append(0 if value is None else value)
        output[name] = sum(values) / len(values) if values else None
        output[name + "_denominator"] = len(values)
    durations = [r.trace.get("stages_ms", {}).get("total", 0) for r in results]
    output["retrieval_p95_ms"] = percentile(
        [
            r.trace.get("retrieval", {}).get("stages_ms", r.trace.get("failed_stages_ms", {})).get("total", 0)
            for r in results
        ]
    )
    output["retrieval_latency_includes_embedding_rewrite_and_rerank"] = True
    output["case_p95_ms"] = percentile(durations)
    output["success_rate"] = (len(cases) - errors) / len(cases) if cases else None
    total_cites = sum(r.metrics.get("citations_total", 0) for r in results)
    valid_cites = sum(r.metrics.get("citations_valid", 0) for r in results)
    output["citations"] = {
        "valid": valid_cites,
        "total": total_cites,
        "rate": valid_cites / total_cites if total_cites else None,
    }
    reviews = [r.human_review for r in results if r.human_review and not r.error]
    facts = sum(r.get("facts_total", 0) for r in reviews)
    supported = sum(r.get("facts_supported", 0) for r in reviews)
    cited = sum(r.get("cited_claims_total", 0) for r in reviews)
    supported_cited = sum(r.get("cited_claims_supported", 0) for r in reviews)
    reviewed_answers = [
        by_case[str(c.pk)].human_review
        for c in answerable
        if str(c.pk) in by_case and by_case[str(c.pk)].human_review.get("answer_pass") is not None
    ]
    refusal_cases = [c for c in cases if c.category == "unanswerable"]
    reviewed_refusals = [
        by_case[str(c.pk)].human_review
        for c in refusal_cases
        if str(c.pk) in by_case and by_case[str(c.pk)].human_review.get("refusal_correct") is not None
    ]
    output["human_review"] = {
        "reviewed": len(reviews),
        "pending": len(cases) - len(reviews),
        "faithfulness": {
            "supported": supported,
            "total": facts,
            "rate": supported / facts if facts else None,
        },
        "citation_support": {
            "supported": supported_cited,
            "total": cited,
            "rate": supported_cited / cited if cited else None,
        },
        "answer_pass": {
            "passed": sum(r["answer_pass"] for r in reviewed_answers),
            "reviewed": len(reviewed_answers),
            "required": len(answerable),
            "rate": sum(r["answer_pass"] for r in reviewed_answers) / len(answerable)
            if answerable and len(reviewed_answers) == len(answerable)
            else None,
        },
        "refusal_correct": {
            "passed": sum(r["refusal_correct"] for r in reviewed_refusals),
            "reviewed": len(reviewed_refusals),
            "required": len(refusal_cases),
            "rate": sum(r["refusal_correct"] for r in reviewed_refusals) / len(refusal_cases)
            if refusal_cases and len(reviewed_refusals) == len(refusal_cases)
            else None,
        },
    }
    return output
