import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import IntegrityError, connection, connections, transaction
from django.utils import timezone

from ai_engine.gateway import embed, generate
from ai_engine.gateway.budget import message_tokens, prepare_input
from ai_engine.gateway.config import GatewayError, profile
from apps.conversations.models import Conversation
from apps.conversations.services import (
    attach_sources,
    begin_run,
    cancel_run,
    model_history,
    prepare_generation,
    save_progress,
)
from apps.knowledge.services import replace_acl
from apps.monitoring.metering import CallMeter, QuotaExceeded, recover_stale_reservations, reserve, settle
from apps.monitoring.models import ModelCallLog, UsageReservation
from apps.users.models import Tenant, User
from common.context import RequestContext

pytestmark = pytest.mark.django_db(transaction=True)
SOURCE = [{"source_id": "S1", "content": "这是公开的测试依据。"}]


@pytest.fixture
def compatible(settings):
    settings.GENERATION_PROVIDER = "compatible"
    settings.GENERATION_BASE_URL = "https://generation.example/v1"
    settings.GENERATION_API_KEY = "generation-test-key"
    settings.GENERATION_MODEL = "chat-test"
    settings.EMBEDDING_PROVIDER = "compatible"
    settings.EMBEDDING_BASE_URL = "https://embedding.example/v1"
    settings.EMBEDDING_API_KEY = "embedding-test-key"
    settings.EMBEDDING_MODEL = "embed-test"


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "ai_engine.gateway.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def completion(text="已依据资料回答 [S1]", usage=None):
    events = [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]
    if usage is not None:
        events.append({"choices": [], "usage": usage})
    wire = "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, text=wire, headers={"Content-Type": "text/event-stream"})


def collect(**kwargs):
    async def run():
        return [item async for item in generate("测试问题", SOURCE, **kwargs)]

    return async_to_sync(run)()


def test_independent_endpoints_credentials_dimensions_and_accounting(
    world, compatible, settings, monkeypatch
):
    settings.EMBEDDING_SEND_DIMENSIONS = True
    calls = []

    def handle(request):
        data = json.loads(request.content)
        calls.append(request.url.host)
        if request.url.host == "embedding.example":
            assert request.headers["Authorization"] == "Bearer embedding-test-key"
            assert data["dimensions"] == 128 and data["encoding_format"] == "float"
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [0.1] * 128}],
                    "usage": {"prompt_tokens": 4, "total_tokens": 4},
                },
            )
        assert request.headers["Authorization"] == "Bearer generation-test-key"
        assert data["max_completion_tokens"] == settings.GENERATION_MAX_OUTPUT_TOKENS
        assert "max_tokens" not in data
        return completion(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    transport(monkeypatch, handle)
    embed(["测试"], ctx=world["ctx"])
    collect(ctx=world["ctx"])
    assert calls == ["embedding.example", "generation.example"]
    assert set(UsageReservation.objects.values_list("charged_tokens", flat=True)) == {4, 15}
    assert UsageReservation.objects.filter(status="reserved").count() == 0
    assert ModelCallLog.objects.count() == 2


def test_rate_limit_retry_releases_rejected_reservation(world, compatible, monkeypatch):
    calls = []

    def handle(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429, json={"error": {"message": "private upstream detail"}}, headers={"Retry-After": "0"}
            )
        return completion(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    transport(monkeypatch, handle)
    collect(ctx=world["ctx"])
    assert len(calls) == 2
    assert UsageReservation.objects.filter(status="released", charged_tokens=0).count() == 1
    assert UsageReservation.objects.filter(status="settled", charged_tokens=15).count() == 1
    assert "private upstream detail" not in json.dumps(list(ModelCallLog.objects.values("usage")))


def test_retry_after_beyond_budget_is_not_ignored(world, compatible, monkeypatch):
    calls = []

    def handle(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "60"})

    transport(monkeypatch, handle)
    with pytest.raises(GatewayError, match="MODEL_RATE_LIMITED"):
        collect(ctx=world["ctx"])
    assert len(calls) == 1
    assert UsageReservation.objects.get().charged_tokens == 0


def test_no_automatic_replay_after_partial_output(world, compatible, monkeypatch):
    calls, closed = [], []

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("upstream secret should not be logged")

        async def aclose(self):
            closed.append(True)

    def handle(request):
        calls.append(1)
        return httpx.Response(200, stream=Broken(), headers={"Content-Type": "text/event-stream"})

    transport(monkeypatch, handle)
    with pytest.raises(GatewayError, match="MODEL_CONNECTION_FAILED"):
        collect(ctx=world["ctx"])
    assert len(calls) == 1 and closed
    row = UsageReservation.objects.get()
    assert row.status == "settled" and row.charged_tokens == row.reserved_tokens
    assert row.usage["quota_basis"] == "reserved_unknown"


def test_missing_usage_is_unknown_and_never_reports_precise_cost(world, compatible, settings, monkeypatch):
    settings.GENERATION_INPUT_USD_PER_MILLION = "1"
    settings.GENERATION_OUTPUT_USD_PER_MILLION = "2"
    transport(monkeypatch, lambda request: completion())
    collect(ctx=world["ctx"])
    row = UsageReservation.objects.get()
    assert row.usage["source"] == "unknown" and row.charged_tokens == row.reserved_tokens
    assert ModelCallLog.objects.get().price_snapshot["estimated_cost_usd"] is None


def test_known_usage_settles_once_and_records_price_snapshot(world, settings):
    settings.GENERATION_INPUT_USD_PER_MILLION = "1"
    settings.GENERATION_OUTPUT_USD_PER_MILLION = "2"
    settings.MODEL_PRICE_VERSION = "test-price-v1"
    row = reserve(world["ctx"], 200, profile("generation"))
    for _ in range(2):
        settle(
            row.pk,
            usage={"source": "provider", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            sent=True,
        )
    row.refresh_from_db()
    assert row.charged_tokens == 15 and ModelCallLog.objects.count() == 1
    cost = ModelCallLog.objects.get().price_snapshot
    assert cost["version"] == "test-price-v1" and Decimal(cost["estimated_cost_usd"]) == Decimal("0.000020")


def test_quota_refuses_before_contacting_provider(world, compatible, monkeypatch):
    Tenant.objects.filter(pk=world["tenant"].pk).update(
        quota_config={"daily_tokens": 0, "user_daily_tokens": 0}
    )
    called = []
    transport(monkeypatch, lambda request: called.append(True))
    with pytest.raises(QuotaExceeded):
        collect(ctx=world["ctx"])
    assert not called and not UsageReservation.objects.exists()


def test_per_user_limit_and_actual_overage_are_preserved(world):
    Tenant.objects.filter(pk=world["tenant"].pk).update(
        quota_config={"daily_tokens": 1000, "user_daily_tokens": 100}
    )
    first = reserve(world["ctx"], 80, profile("generation"))
    with pytest.raises(QuotaExceeded):
        reserve(world["ctx"], 21, profile("generation"))
    settle(first.pk, usage={"source": "provider", "total_tokens": 120}, sent=True)
    with pytest.raises(QuotaExceeded):
        reserve(world["ctx"], 1, profile("generation"))
    first.refresh_from_db()
    assert first.charged_tokens == 120


def test_expired_reservations_are_recovered_without_double_charge(world):
    before_send = reserve(world["ctx"], 100, profile("generation"))
    sent = reserve(world["ctx"], 200, profile("generation"))
    UsageReservation.objects.filter(pk=sent.pk).update(dispatched=True)
    UsageReservation.objects.all().update(expires_at=timezone.now() - timedelta(seconds=1))
    recover_stale_reservations()
    recover_stale_reservations()
    before_send.refresh_from_db()
    sent.refresh_from_db()
    assert before_send.status == "released" and before_send.charged_tokens == 0
    assert sent.status == "settled" and sent.charged_tokens == 200
    assert ModelCallLog.objects.count() == 2


def test_expired_or_settled_reservation_cannot_start_an_upstream_call(world):
    meter = CallMeter(world["ctx"], profile("generation"), 100, 100)
    meter.start()
    UsageReservation.objects.filter(pk=meter.row.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(GatewayError, match="MODEL_RESERVATION_EXPIRED"):
        meter.mark_sent()
    assert not meter.sent
    recover_stale_reservations()
    with pytest.raises(GatewayError, match="MODEL_RESERVATION_EXPIRED"):
        meter.mark_sent()
    meter.row.refresh_from_db()
    assert meter.row.status == "released" and meter.row.charged_tokens == 0


def test_database_guards_usage_and_call_log_tenant_scope(world):
    row = reserve(world["ctx"], 100, profile("generation"))
    with pytest.raises(IntegrityError), transaction.atomic():
        UsageReservation.objects.filter(pk=row.pk).update(tenant=world["other"])
    with pytest.raises(IntegrityError), transaction.atomic():
        ModelCallLog.objects.create(
            tenant=world["other"],
            reservation=row,
            trace_id="bad",
            task_type="generation",
            model="test",
            status="failed",
        )
    c = Conversation.objects.create(tenant=world["tenant"], user=world["admin"])
    run, _ = begin_run(world["ctx"], c.pk, "问题", "scope-check")
    with pytest.raises(IntegrityError), transaction.atomic():
        ModelCallLog.objects.create(
            tenant=world["other"],
            run=run,
            trace_id="bad",
            task_type="generation",
            model="test",
            status="failed",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        reserve(RequestContext.for_user(world["member"]), 10, profile("generation"), run=run)


def test_concurrent_admission_cannot_overspend_tenant(world):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock admission requires PostgreSQL")
    Tenant.objects.filter(pk=world["tenant"].pk).update(
        quota_config={"daily_tokens": 100, "user_daily_tokens": 100}
    )
    barrier = threading.Barrier(2)

    def compete(_):
        try:
            user = User.objects.select_related("tenant").get(pk=world["admin"].pk)
            barrier.wait(timeout=5)
            reserve(RequestContext.for_user(user), 80, profile("generation"))
            return "accepted"
        except QuotaExceeded:
            return "denied"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(compete, range(2)))
    assert sorted(outcomes) == ["accepted", "denied"]
    assert UsageReservation.objects.count() == 1


def test_concurrent_settlement_is_exactly_once(world):
    if connection.vendor != "postgresql":
        pytest.skip("Concurrent settlement uses PostgreSQL row locks")
    row = reserve(world["ctx"], 100, profile("generation"))
    barrier = threading.Barrier(2)

    def finish(_):
        try:
            barrier.wait(timeout=5)
            settle(row.pk, usage={"source": "provider", "total_tokens": 10}, sent=True)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(finish, range(2)))
    row.refresh_from_db()
    assert row.charged_tokens == 10 and ModelCallLog.objects.filter(reservation=row).count() == 1


def test_cancelled_real_adapter_closes_and_settles_in_flight_budget(
    world, compatible, settings, ingest, monkeypatch
):
    settings.EMBEDDING_PROVIDER = "demo"
    ingest()
    run, _ = begin_run(
        world["ctx"],
        Conversation.objects.create(tenant=world["tenant"], user=world["admin"]).pk,
        "退款",
        "metered-cancel",
    )
    closed = []

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.sleep(30)

        async def aclose(self):
            closed.append(True)

    transport(
        monkeypatch,
        lambda request: httpx.Response(200, stream=Slow(), headers={"Content-Type": "text/event-stream"}),
    )
    from apps.conversations.stream import stream_events

    async def consume():
        async for item in stream_events(world["ctx"], run):
            if "event: delta" in item:
                await sync_to_async(cancel_run)(world["ctx"], run.pk)

    async_to_sync(consume)()
    run.refresh_from_db()
    row = UsageReservation.objects.get(run=run, task_type="generation")
    assert run.status == "cancelled" and run.output_message.content == "partial" and closed
    assert row.status == "settled" and row.charged_tokens == row.reserved_tokens
    assert ModelCallLog.objects.get(reservation=row).status == "interrupted"
    assert not UsageReservation.objects.filter(run=run, status="reserved").exists()


def test_ingestion_reports_quota_failure_explicitly(world, ingest):
    Tenant.objects.filter(pk=world["tenant"].pk).update(
        quota_config={"daily_tokens": 0, "user_daily_tokens": 0}
    )
    doc, _, _, task = ingest()
    task.refresh_from_db()
    doc.refresh_from_db()
    assert task.status == "FAILED" and task.error_code == "QUOTA_EXCEEDED"
    assert doc.active_build_id is None


def test_budget_keeps_question_and_fits_evidence_and_recent_history(settings):
    settings.GENERATION_INPUT_BUDGET = 1800
    settings.GENERATION_HISTORY_BUDGET = 500
    sources = [{"source_id": "S1", "content": "x" * 3000}, {"source_id": "S2", "content": "小段证据"}]
    pairs = [
        [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}],
        [{"role": "user", "content": "最近问题"}, {"role": "assistant", "content": "最近回答"}],
    ]
    prepared = prepare_input("当前问题", sources, pairs)
    assert [s["source_id"] for s in prepared.sources] == ["S2"]
    assert message_tokens(prepared.messages) <= 1800
    assert prepared.messages[-1]["role"] == "user" and "当前问题" in prepared.messages[-1]["content"]
    with pytest.raises(GatewayError, match="QUESTION_TOKEN_BUDGET_EXCEEDED"):
        prepare_input("x" * 5000, sources)


def test_model_history_excludes_revoked_and_outdated_answers(world, ingest):
    doc, _, _, _ = ingest(kb=world["private"])
    replace_acl(world["ctx"], world["private"], [{"user_id": world["member"].pk, "access_level": "read"}])
    ctx = RequestContext.for_user(world["member"])
    c = Conversation.objects.create(tenant=world["tenant"], user=world["member"])
    first, _ = begin_run(ctx, c.pk, "退款", "history-first")
    from ai_engine.retrieval import search

    attach_sources(ctx, first, search(ctx, "退款"))
    save_progress(first.pk, "旧资料的秘密答案 [S1]", "completed")
    second, _ = begin_run(ctx, c.pk, "其他问题", "history-next")
    assert model_history(ctx, second) and "[S1]" not in model_history(ctx, second)[0][-1]["content"]
    ingest(text="新版退款政策。", document_id=doc.pk, kb=world["private"])
    assert model_history(ctx, second) == []
    replace_acl(world["ctx"], world["private"], [])
    assert model_history(ctx, second) == []


def test_revoked_history_never_reaches_prepared_prompt(world, ingest):
    ingest(kb=world["private"])
    replace_acl(world["ctx"], world["private"], [{"user_id": world["member"].pk, "access_level": "read"}])
    ctx = RequestContext.for_user(world["member"])
    c = Conversation.objects.create(tenant=world["tenant"], user=world["member"])
    first, _ = begin_run(ctx, c.pk, "旧问题", "hidden-first")
    from ai_engine.retrieval import search

    attach_sources(ctx, first, search(ctx, "退款"))
    save_progress(first.pk, "不得发送给模型的旧秘密", "completed")
    replace_acl(world["ctx"], world["private"], [])
    second, _ = begin_run(ctx, c.pk, "本轮公开问题", "hidden-next")
    prepared = prepare_generation(ctx, second, SOURCE)
    assert "旧秘密" not in json.dumps(prepared.messages, ensure_ascii=False)
    assert prepared.history_messages == 0


def test_model_status_hides_credentials_and_quota_is_tenant_scoped(world, compatible, client_for):
    client = client_for(world["admin"])
    status = client.get("/api/v1/monitoring/models")
    assert status.status_code == 200
    assert (
        "generation-test-key" not in status.content.decode()
        and "generation.example" not in status.content.decode()
    )
    assert client_for(world["member"]).get("/api/v1/monitoring/models").status_code == 403
    assert (
        client.put(
            "/api/v1/monitoring/quota", {"daily_tokens": 200, "user_daily_tokens": 150}, format="json"
        ).status_code
        == 200
    )
    assert client_for(world["member"]).put("/api/v1/monitoring/quota", {}, format="json").status_code == 403
    reserve(world["ctx"], 100, profile("generation"))
    assert client.get("/api/v1/monitoring/quota").json()["tenant_committed"] == 100
    assert client_for(world["outsider"]).get("/api/v1/monitoring/quota").json()["tenant_committed"] == 0


def test_probe_refuses_schema_mismatch_without_network(world, compatible, settings, client_for, monkeypatch):
    settings.EMBEDDING_DIMENSIONS = 256
    calls = []
    transport(monkeypatch, lambda request: calls.append(True))
    response = client_for(world["admin"]).post(
        "/api/v1/monitoring/models", {"kind": "embedding"}, format="json"
    )
    assert response.json()["error_code"] == "EMBEDDING_SCHEMA_MISMATCH" and not calls


def test_legacy_credentials_are_not_forwarded_to_an_override_host(settings):
    settings.AI_API_KEY = "legacy-private-key"
    settings.EMBEDDING_PROVIDER = "compatible"
    settings.EMBEDDING_BASE_URL = "http://other-provider.example/v1"
    settings.EMBEDDING_API_KEY = ""
    settings.EMBEDDING_AUTH_REQUIRED = False
    assert profile("embedding").headers() == {}
    settings.EMBEDDING_AUTH_REQUIRED = True
    with pytest.raises(GatewayError, match="MODEL_NOT_CONFIGURED"):
        profile("embedding")


def test_profile_repr_never_exposes_keys_or_endpoint(compatible):
    value = repr(profile("generation"))
    assert "generation-test-key" not in value and "generation.example" not in value


def test_rest_gateway_error_is_redacted(world, compatible, settings, ingest, client_for, monkeypatch):
    settings.EMBEDDING_PROVIDER = "demo"
    ingest()
    settings.EMBEDDING_PROVIDER = "compatible"
    transport(
        monkeypatch, lambda request: httpx.Response(401, json={"error": {"message": "private-upstream-body"}})
    )
    response = client_for(world["admin"]).post("/api/v1/retrieval/search", {"query": "退款"}, format="json")
    assert response.status_code == 503 and response.json()["error"] == "MODEL_AUTH_FAILED"
    assert (
        "private-upstream-body" not in response.content.decode()
        and "embedding-test-key" not in response.content.decode()
    )


def test_generation_deadline_and_cancellation_close_stream(world, compatible, settings, monkeypatch):
    settings.GENERATION_TIMEOUT_SECONDS = 1
    settings.MODEL_MAX_RETRIES = 0
    closed = []

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(3)
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            closed.append(True)

    transport(
        monkeypatch,
        lambda request: httpx.Response(200, stream=Slow(), headers={"Content-Type": "text/event-stream"}),
    )
    with pytest.raises(GatewayError, match="MODEL_TIMEOUT"):
        collect(ctx=world["ctx"])
    assert closed
    assert not UsageReservation.objects.filter(status="reserved").exists()


@pytest.mark.parametrize(
    "rows",
    [
        [{"index": True, "embedding": [1.0] * 128}],
        [{"index": 0, "embedding": [True] * 128}],
        [{"index": 0, "embedding": [1e100] * 128}],
        [{"index": 2, "embedding": [1.0] * 128}],
    ],
)
def test_invalid_embedding_payloads_are_rejected(compatible, monkeypatch, rows):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"data": rows}))
    with pytest.raises(GatewayError):
        embed(["测试"])
