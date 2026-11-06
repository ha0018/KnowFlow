import asyncio
import json

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.test import AsyncClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.conversations.models import Conversation, Run
from apps.conversations.services import attach_sources, begin_run, cancel_run, history, save_progress
from apps.conversations.stream import stream_events
from apps.knowledge.services import replace_acl
from common.context import RequestContext

pytestmark = pytest.mark.django_db(transaction=True)


async def collect(iterator):
    return "".join([item async for item in iterator])


def test_authenticated_sse_contract_and_reference_access(world, ingest, client_for):
    ingest()
    c = Conversation.objects.create(tenant=world["tenant"], user=world["member"])
    token = str(RefreshToken.for_user(world["member"]).access_token)

    async def request():
        response = await AsyncClient().post(
            f"/api/v1/conversations/{c.pk}/messages:stream",
            data=json.dumps({"question": "退款超过30天还能申请吗？"}),
            content_type="application/json",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "contract-one"},
        )
        assert response.status_code == 200
        return b"".join([item async for item in response.streaming_content]).decode()

    wire = async_to_sync(request)()
    assert all(f"event: {name}" in wire for name in ["meta", "references", "delta", "done"])
    run = Run.objects.get(conversation=c)
    assert run.status == "completed" and "30" in run.output_message.content
    ref = run.output_message.references.first()
    assert client_for(world["member"]).get(f"/api/v1/references/{ref.pk}").status_code == 200
    source_event = next(block for block in wire.split("\n\n") if block.startswith("event: references"))
    streamed_ids = [source["id"] for source in json.loads(source_event.split("data: ", 1)[1])["sources"]]
    history_response = client_for(world["member"]).get(f"/api/v1/conversations/{c.pk}/messages").json()
    assert [ref["id"] for ref in history_response[-1]["references"]] == streamed_ids
    assert client_for(world["outsider"]).get(f"/api/v1/references/{ref.pk}").status_code == 404


def test_repeated_idempotency_key_does_not_create_messages(world):
    ctx = world["ctx"]
    c = Conversation.objects.create(tenant=world["tenant"], user=world["admin"])
    first, created = begin_run(ctx, c.pk, "退款？", "same-key")
    second, created_again = begin_run(ctx, c.pk, "退款？", "same-key")
    assert created and not created_again and first.pk == second.pk
    assert c.messages.count() == 2
    from apps.conversations.services import Conflict

    with pytest.raises(Conflict):
        begin_run(ctx, c.pk, "不同问题", "same-key")


def test_history_hides_whole_answer_and_reference_after_revocation(world, ingest, client_for):
    doc, _, _, _ = ingest(kb=world["private"])
    replace_acl(world["ctx"], world["private"], [{"user_id": world["member"].pk, "access_level": "read"}])
    ctx = RequestContext.for_user(world["member"])
    c = Conversation.objects.create(tenant=world["tenant"], user=world["member"])
    run, _ = begin_run(ctx, c.pk, "退款", "history")
    from ai_engine.retrieval import search

    sources = search(ctx, "退款")
    attach_sources(ctx, run, sources)
    save_progress(run.pk, "仅限授权人员的答案", "completed")
    ref = run.output_message.references.first()
    replace_acl(world["ctx"], world["private"], [])
    rows = history(ctx, c)
    assert rows[-1]["status"] == "redacted" and "仅限授权人员" not in rows[-1]["content"]
    assert rows[-1]["references"] == []
    client = client_for(world["member"])
    assert client.get(f"/api/v1/references/{ref.pk}").status_code == 404
    assert "仅限授权人员" not in client.get(f"/api/v1/runs/{run.pk}").json()["content"]


def test_cancel_closes_upstream_and_preserves_partial_answer(world, ingest, monkeypatch):
    ingest()
    ctx = world["ctx"]
    c = Conversation.objects.create(tenant=world["tenant"], user=world["admin"])
    run, _ = begin_run(ctx, c.pk, "退款", "cancel")
    closed = []

    async def fake(question, sources, **kwargs):
        try:
            yield {"text": "部分答案"}
            await asyncio.sleep(30)
        finally:
            closed.append(True)

    monkeypatch.setattr("apps.conversations.stream.generate", fake)

    async def consume():
        wire = []
        async for item in stream_events(ctx, run):
            wire.append(item)
            if "event: delta" in item:
                await sync_to_async(cancel_run)(ctx, run.pk)
        return "".join(wire)

    wire = async_to_sync(consume)()
    run.refresh_from_db()
    assert run.status == "cancelled" and closed
    assert "cancelled" in wire
    assert run.output_message.content == "部分答案"


def test_model_error_persists_failed_terminal(world, ingest, monkeypatch):
    ingest()
    c = Conversation.objects.create(tenant=world["tenant"], user=world["admin"])
    run, _ = begin_run(world["ctx"], c.pk, "退款", "failure")

    async def broken(question, sources, **kwargs):
        yield {"text": "已生成"}
        raise RuntimeError("secret upstream details")

    monkeypatch.setattr("apps.conversations.stream.generate", broken)
    wire = async_to_sync(collect)(stream_events(world["ctx"], run))
    run.refresh_from_db()
    assert run.status == "failed" and run.output_message.content == "已生成"
    assert "secret upstream" not in wire and "event: error" in wire


def test_no_evidence_refuses(world):
    c = Conversation.objects.create(tenant=world["tenant"], user=world["admin"])
    run, _ = begin_run(world["ctx"], c.pk, "冥王星天气", "refusal")
    wire = async_to_sync(collect)(stream_events(world["ctx"], run))
    assert "没有找到足够依据" in wire
