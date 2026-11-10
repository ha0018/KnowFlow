import hashlib
import json
from collections import Counter
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings as config
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction

from ai_engine.gateway.config import GatewayError
from ai_engine.retrieval import search
from ai_engine.retrieval.adapters import rerank, rewrite_query
from ai_engine.retrieval.options import baseline
from apps.documents.models import Document, DocumentChunk
from apps.evaluation.configuration import snapshot
from apps.evaluation.corpus import verify_frozen
from apps.evaluation.metrics import aggregate, citation_metrics, retrieval_metrics
from apps.evaluation.models import EvalCase, EvalDataset, EvalResult, EvalRun
from apps.evaluation.services import perform_evaluation
from apps.knowledge.models import KnowledgeBaseACL
from apps.monitoring.models import ModelCallLog, UsageReservation
from apps.users.models import User
from common.context import RequestContext

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def quality(world):
    call_command("seed_quality", email=world["admin"].email, password="test-password-2026", stdout=StringIO())
    return EvalDataset.objects.get(tenant=world["tenant"], version="quality-v1")


def test_frozen_dataset_exact_split_spans_and_idempotent_import(world, quality, tmp_path):
    raw = Path(config.BASE_DIR.parent / "docs/datasets/quality-v1.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == quality.manifest["source_sha256"]
    assert quality.cases.count() == 100
    with pytest.raises(CommandError, match="Dataset not found"):
        call_command(
            "evaluate_demo",
            email=world["admin"].email,
            output=str(tmp_path / "legacy.json"),
            stdout=StringIO(),
        )
    unscoped = EvalRun.objects.create(
        tenant=world["tenant"],
        dataset=quality,
        actor=world["admin"],
        trace_id="no-implicit-holdout",
        config_snapshot={"mode": "hybrid"},
    )
    perform_evaluation(unscoped.pk)
    unscoped.refresh_from_db()
    assert unscoped.metrics["error"] == "EVALUATION_SPLIT_REQUIRED" and not unscoped.results.exists()
    assert Counter(quality.cases.filter(split="tuning").values_list("category", flat=True)) == {
        "answerable": 28,
        "unanswerable": 6,
        "permission": 4,
        "injection": 2,
    }
    assert Counter(quality.cases.filter(split="holdout").values_list("category", flat=True)) == {
        "answerable": 42,
        "unanswerable": 9,
        "permission": 6,
        "injection": 3,
    }
    for case in quality.cases.filter(category="answerable"):
        for gold in case.gold_evidence:
            doc = Document.objects.get(pk=gold["document_id"])
            text = doc.active_build.parsed_blocks[gold["block"]]["text"]
            assert text[gold["char_start"] : gold["char_end"]] == gold["quote"]
            assert "chunk_id" not in gold
    call_command("seed_quality", email=world["admin"].email, password="unused-password", stdout=StringIO())
    assert quality.cases.count() == 100
    verify_frozen(quality)
    # A changed non-gold document must invalidate the whole frozen corpus too.
    doc = Document.objects.get(pk=quality.manifest["fixture_documents"]["private"])
    Document.objects.filter(pk=doc.pk).update(active_build=None)
    with pytest.raises(ValueError, match="FROZEN_CORPUS_CHANGED"):
        verify_frozen(quality)


def test_evidence_metrics_keep_chunk_rank_and_require_correct_version_and_span():
    gold = [
        {"document_id": "d", "version_id": "v", "sha256": "h", "block": 3, "char_start": 10, "char_end": 20}
    ]
    wrong = {
        "document_id": "d",
        "version_id": "v",
        "sha256": "h",
        "locator": {"block": 1, "char_start": 0, "char_end": 100},
    }
    right = {**wrong, "locator": {"block": 3, "char_start": 0, "char_end": 30}}
    metric = retrieval_metrics([wrong, wrong, right], gold)
    assert metric == {
        "doc_recall_at_5": 1,
        "evidence_hit_at_5": 1,
        "mrr_at_5": 1 / 3,
        "precision_at_5": 1 / 5,
        "evidence_annotations_complete": True,
    }
    assert retrieval_metrics([wrong], gold)["evidence_hit_at_5"] == 0
    assert retrieval_metrics([{**right, "version_id": "stale"}], gold)["doc_recall_at_5"] == 0
    assert retrieval_metrics([right], gold + [{**gold[0], "block": 9}])["evidence_hit_at_5"] == 0
    assert (
        citation_metrics("依据[S1]，伪来源[S99]", [{"source_id": "S1", "locator": {"block": 0}}])[
            "citation_validity"
        ]
        == 0.5
    )


def test_errors_stay_in_quality_and_latency_denominators_and_no_fake_human_score():
    cases = [
        SimpleNamespace(pk="ok", category="answerable"),
        SimpleNamespace(pk="bad", category="answerable"),
    ]
    rows = [
        SimpleNamespace(
            case_id="ok",
            metrics={"doc_recall_at_5": 1},
            error="",
            trace={"stages_ms": {"total": 5}},
            human_review={},
        ),
        SimpleNamespace(
            case_id="bad",
            metrics={"doc_recall_at_5": 1},
            error="TIMEOUT",
            trace={"stages_ms": {"total": 1000}},
            human_review={},
        ),
    ]
    metrics = aggregate(rows, cases, provider="demo")
    assert metrics["doc_recall_at_5"] == 0.5
    assert metrics["case_p95_ms"] == 1000 and metrics["success_rate"] == 0.5
    assert metrics["human_review"]["faithfulness"]["rate"] is None
    assert metrics["human_review"]["answer_pass"]["rate"] is None


@pytest.mark.parametrize("name", ["B0", "B1", "B2"])
def test_each_pipeline_preserves_tenant_and_private_scope(world, ingest, name):
    public, *_ = ingest()
    private, *_ = ingest(text="退款机密代号TOP-SECRET，核验金额83万。", kb=world["private"])
    ctx = RequestContext.for_user(world["member"])
    trace = {}
    hits = search(ctx, "退款机密", options=replace(baseline(name), rewrite=True), diagnostics=trace)
    assert str(private.pk) not in {h["document_id"] for h in hits}
    assert all(h["document_id"] == str(public.pk) for h in hits)
    assert trace["stages_ms"]["total"] >= 0
    assert {"embedding", "dense", "keyword", "fusion", "rerank", "rewrite"} <= trace["stages_ms"].keys()


def test_revocation_during_rerank_never_returns_stale_candidates(world, ingest, monkeypatch):
    ingest(kb=world["private"])
    grant = KnowledgeBaseACL.objects.create(
        tenant=world["tenant"], knowledge_base=world["private"], user=world["member"], access_level="read"
    )

    def revoke(ctx, query, rows, **kwargs):
        grant.delete()
        return rows, {}

    monkeypatch.setattr("ai_engine.retrieval.rerank", revoke)
    with pytest.raises(GatewayError, match="PERMISSION_CHANGED"):
        search(RequestContext.for_user(world["member"]), "退款", options=baseline("B2"))


def configure_reranker(settings):
    settings.RERANKER_PROVIDER = "compatible"
    settings.RERANKER_BASE_URL = "http://rerank.test/v1"
    settings.RERANKER_API_KEY = "rerank-private-key"
    settings.RERANKER_MODEL = "fixture-reranker-v1"


def test_portable_dense_ranking_is_cosine_not_unnormalized_dot_product(world, ingest, monkeypatch):
    if connection.vendor != "sqlite":
        pytest.skip("Portable exact implementation only")
    correct, *_ = ingest(text="退款政策正确方向。", name="correct.md")
    scaled, *_ = ingest(text="退款政策较大模长。", name="scaled.md")
    vector = [1.0] + [0.0] * (config.EMBEDDING_DIMENSIONS - 1)
    DocumentChunk.objects.filter(document=correct).update(embedding=vector)
    DocumentChunk.objects.filter(document=scaled).update(embedding=[100.0, 100.0] + [0.0] * (len(vector) - 2))
    monkeypatch.setattr("ai_engine.retrieval.embed", lambda *a, **kw: ([vector], {"source": "unknown"}))
    hits = search(world["ctx"], "退款政策", top_k=1, options=baseline("B0"))
    assert hits[0]["document_id"] == str(correct.pk)


def test_remote_reranker_validates_permutation_keeps_keys_separate_and_accounts(
    world, ingest, settings, monkeypatch
):
    doc, *_ = ingest()
    row = DocumentChunk.objects.filter(document=doc).first()
    configure_reranker(settings)
    settings.AI_API_KEY = "legacy-key-must-not-travel"
    seen = []

    async def response(model, path, body, remaining):
        seen.append((model.headers(), body))
        return {"results": [{"index": 0, "relevance_score": 0.7}], "usage": {"total_tokens": 12}}

    monkeypatch.setattr("ai_engine.retrieval.adapters.json_request", response)
    rows, scores = rerank(world["ctx"], "退款", [row])
    assert rows == [row] and scores[row.pk] == 0.7
    assert seen[0][0] == {"Authorization": "Bearer rerank-private-key"}
    log = ModelCallLog.objects.get(task_type="rerank")
    assert log.usage["quota_basis"] == "provider" and log.usage["total_tokens"] == 12
    assert not UsageReservation.objects.filter(status="reserved").exists()

    async def invalid(*args):
        return {"results": [{"index": 7, "relevance_score": 0.7}]}

    monkeypatch.setattr("ai_engine.retrieval.adapters.json_request", invalid)
    with pytest.raises(GatewayError, match="RERANKER_INDEX_INVALID"):
        rerank(world["ctx"], "退款", [row])
    assert ModelCallLog.objects.filter(task_type="rerank", status="failed").count() == 1


def test_remote_rewrite_strict_json_preserves_identifiers_and_meters(world, settings, monkeypatch):
    settings.REWRITE_PROVIDER = "generation"
    settings.GENERATION_PROVIDER = "compatible"
    settings.GENERATION_BASE_URL = "http://rewrite.test/v1"
    settings.GENERATION_MODEL = "fixture-generation"
    settings.GENERATION_API_KEY = "rewrite-key"
    seen = []

    async def response(model, path, body, remaining):
        seen.append(body)
        return {
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"query":"KF-204的退款政策是什么？"}'}}
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }

    monkeypatch.setattr("ai_engine.retrieval.adapters.json_request", response)
    assert "KF-204" in rewrite_query(world["ctx"], "KF-204退款规则？")
    assert "expected_answer" not in json.dumps(seen)
    assert ModelCallLog.objects.get(task_type="rewrite").usage["total_tokens"] == 30
    with pytest.raises(GatewayError, match="REWRITE_IDENTIFIER_CHANGED"):
        rewrite_query(world["ctx"], "KF-999退款规则？")


def test_quality_evaluation_api_default_tuning_review_and_revocation(world, quality, client_for):
    client = client_for(world["admin"])
    response = client.post(
        "/api/v1/evaluations/runs",
        {"dataset_id": str(quality.pk), "baseline": "B2", "answers": True},
        format="json",
    )
    assert response.status_code == 202
    run = EvalRun.objects.get(pk=response.json()["id"])
    assert run.status == "COMPLETED" and run.config_snapshot["split"] == "tuning"
    assert run.results.count() == 40 and not run.results.filter(case__split="holdout").exists()
    data = client.get(f"/api/v1/evaluations/runs/{run.pk}").json()
    assert len(data["results"]) == 40
    row = run.results.filter(case__category="answerable").first()
    review_url = f"/api/v1/evaluations/runs/{run.pk}/results/{row.pk}/review"
    assert (
        client.put(
            review_url, {"facts_total": 1, "facts_supported": 2, "notes": "invalid"}, format="json"
        ).status_code
        == 400
    )
    assert (
        client.put(
            review_url,
            {
                "answer_pass": False,
                "facts_total": 1,
                "facts_supported": 1,
                "notes": "仅验证保存路径，未执行正式人工质量验收",
            },
            format="json",
        ).status_code
        == 200
    )
    assert client_for(world["outsider"]).get(f"/api/v1/evaluations/runs/{run.pk}").status_code == 404
    reader = User.objects.get(pk=quality.manifest["actors"]["reader"])
    # Restrict the KB after evaluation. The administrator can still access it,
    # but the original case actor can no longer read those saved evidence rows.
    from apps.knowledge.models import KnowledgeBase

    KnowledgeBase.objects.filter(pk__in=quality.manifest["kb_ids"]).update(visibility="private")
    assert not reader.is_tenant_admin
    new = client.get(f"/api/v1/evaluations/runs/{run.pk}").json()
    assert any(r["redacted"] and not r["answer"] and not r["sources"] for r in new["results"])
    assert client.put(review_url, {"notes": "不得继续复核"}, format="json").status_code == 400


def test_evaluation_tenant_and_dataset_guards_survive_schema_upgrade(world):
    dataset = EvalDataset.objects.create(
        tenant=world["tenant"], name="a", version="v1", corpus_manifest_hash="x"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        EvalCase.objects.create(tenant=world["other"], dataset=dataset, question="forged")
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    other = EvalDataset.objects.create(
        tenant=world["tenant"], name="b", version="v1", corpus_manifest_hash="x"
    )
    case = EvalCase.objects.create(tenant=world["tenant"], dataset=other, question="q")
    run = EvalRun.objects.create(
        tenant=world["tenant"], dataset=dataset, actor=world["admin"], trace_id="scope"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        EvalResult.objects.create(tenant=world["tenant"], run=run, case=case)


def test_frozen_config_change_fails_before_any_retrieval(world, quality, settings):
    run = EvalRun.objects.create(
        tenant=world["tenant"],
        dataset=quality,
        actor=world["admin"],
        trace_id="config",
        config_snapshot=snapshot(quality),
    )
    settings.RERANKER_MODEL = "changed-after-enqueue"
    perform_evaluation(run.pk)
    run.refresh_from_db()
    assert run.status == "FAILED" and run.metrics["error"] == "EVALUATION_CONFIG_CHANGED"
    assert not run.results.exists()
