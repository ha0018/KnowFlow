from apps.documents.selectors import active_chunks

from .services import case_context, verify_gold


def result_data(ctx, row):
    ids = set(row.retrieved_ids) | {
        c["chunk_id"] for c in row.trace.get("retrieval", {}).get("candidates", [])
    }
    visible = active_chunks(ctx).filter(pk__in=ids).count() == len(ids)
    try:
        subject = case_context(row.run, row.case)
        visible = visible and active_chunks(subject).filter(pk__in=ids).count() == len(ids)
        verify_gold(subject, row.case)
        verify_gold(ctx, row.case)
    except Exception:
        visible = False
    return {
        "id": str(row.pk),
        "case_id": str(row.case_id),
        "case_key": row.case.case_key,
        "question": row.case.question,
        "category": row.case.category,
        "split": row.case.split,
        "expected_answer": row.case.expected_answer if visible else "已撤权，内容已隐藏。",
        "review_guidance": row.case.spec if visible else {},
        "gold_evidence": row.case.gold_evidence if visible else [],
        "retrieved_ids": row.retrieved_ids if visible else [],
        "sources": row.retrieval_snapshot if visible else [],
        "answer": row.answer if visible else "",
        "metrics": row.metrics if visible else {},
        "trace": row.trace if visible else {},
        "human_review": row.human_review if visible else {},
        "error": row.error if visible else "EVIDENCE_NO_LONGER_ACCESSIBLE",
        "redacted": not visible,
    }


def run_report(ctx, run):
    return {
        "run_id": str(run.pk),
        "status": run.status,
        "code_revision": run.code_revision,
        "config": run.config_snapshot,
        "metrics": run.metrics,
        "trace_id": run.trace_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "results": [
            result_data(ctx, r)
            for r in run.results.select_related("case", "run__actor", "run__dataset").order_by(
                "case__case_key", "id"
            )
        ],
    }
