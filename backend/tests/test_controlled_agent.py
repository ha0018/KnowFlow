import asyncio
import json
import sys
import time

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import IntegrityError, transaction
from django.test import AsyncClient
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.tokens import RefreshToken

from ai_engine.gateway.config import GatewayError
from apps.agents.models import BusinessIssue, ToolCall
from apps.agents.runner import AgentSession
from apps.agents.services import execute, invoke_tool
from apps.conversations.models import Conversation
from apps.conversations.services import begin_run, cancel_run, history
from apps.knowledge.services import replace_acl
from apps.monitoring.models import UsageReservation
from common.context import RequestContext

pytestmark = pytest.mark.django_db(transaction=True)


def start(world, question, key="agent", user=None, **kwargs):
    user = user or world["admin"]
    user.refresh_from_db()
    ctx = RequestContext.for_user(user)
    conversation = Conversation.objects.create(tenant=user.tenant, user=user)
    run, _ = begin_run(ctx, conversation.pk, question, key, kind="agent", **kwargs)
    return ctx, run


async def consume(session):
    return "".join([item async for item in session.stream()])


@pytest.mark.parametrize("tool", ["search", "summary", "analysis"])
def test_three_real_process_tools_end_to_end_with_references(world, ingest, client_for, tool):
    doc, *_ = ingest()
    if tool == "analysis":
        from django.utils import timezone

        BusinessIssue.objects.create(
            tenant=world["tenant"],
            occurred_at=timezone.now(),
            product="KnowFlow",
            category="缺陷",
            description="own",
        )
        BusinessIssue.objects.create(
            tenant=world["other"],
            occurred_at=timezone.now(),
            product="KnowFlow",
            category="缺陷",
            description="foreign",
        )
        question = "统计2026-01-01至2026-12-31按类别分组的问题"
    else:
        question = f"总结文档 {doc.pk}" if tool == "summary" else "退款超过30天还能申请吗？"
    ctx, run = start(world, question)
    wire = async_to_sync(consume)(AgentSession(ctx, run))
    run.refresh_from_db()
    assert run.status == "completed", (run.error_code, wire)
    assert "event: tool" in wire and "event: references" in wire and "event: done" in wire
    assert run.toolcall_set.get().status == "completed"
    assert not UsageReservation.objects.filter(run=run, status="reserved").exists()
    data = client_for(world["admin"]).get(f"/api/v1/runs/{run.pk}").json()
    assert data["references"] and "[S1]" in data["content"]
    ref = data["references"][0]
    endpoint = "agents/references" if ref.get("kind") == "analysis" else "references"
    source = client_for(world["admin"]).get(f"/api/v1/{endpoint}/{ref['id']}")
    assert source.status_code == 200
    assert client_for(world["outsider"]).get(f"/api/v1/{endpoint}/{ref['id']}").status_code == 404
    if tool == "analysis":
        assert json.loads(source.json()["excerpt"])["total"] == 1
    else:
        assert "退款" in source.json()["excerpt"]


INVALID = [
    ("ShellTool", {"command": "whoami"}),
    ("PythonTool", {"code": "print(1)"}),
    ("SQLTool", {"sql": "SELECT 1"}),
    ("URLTool", {"url": "https://invalid"}),
    ("KnowledgeSearchTool", {"query": "x", "tenant_id": "forged"}),
    ("KnowledgeSearchTool", {"query": "x", "user_id": "forged"}),
    ("KnowledgeSearchTool", {"query": "x", "url": "file:///secret"}),
    ("KnowledgeSearchTool", {"query": "x", "top_k": True}),
    ("KnowledgeSearchTool", {"query": "x", "top_k": 1.1}),
    ("KnowledgeSearchTool", {"query": "x", "top_k": "5"}),
    ("KnowledgeSearchTool", {"query": "x", "top_k": 0}),
    ("KnowledgeSearchTool", {"query": "x", "top_k": 9}),
    ("KnowledgeSearchTool", {"query": "x", "kb_ids": "all"}),
    ("KnowledgeSearchTool", {"query": "x", "kb_ids": [12]}),
    ("KnowledgeSearchTool", {"query": "x", "kb_ids": ["bad"]}),
    ("KnowledgeSearchTool", {"query": "x", "kb_ids": ["same", "same"]}),
    ("KnowledgeSearchTool", {"query": ""}),
    ("KnowledgeSearchTool", {"query": 42}),
    ("KnowledgeSearchTool", '{"query":"x","query":"y"}'),
    ("KnowledgeSearchTool", '{"query":"x","top_k":NaN}'),
    ("KnowledgeSearchTool", "not-json"),
    ("KnowledgeSearchTool", []),
    ("KnowledgeSearchTool", None),
    ("DocumentSummaryTool", {"document_id": "bad"}),
    ("DocumentSummaryTool", {"document_id": 123}),
    ("DocumentSummaryTool", {"document_id": "bad", "path": "/secret"}),
    ("DataAnalysisTool", {"date_from": "2026-12-01", "date_to": "2026-01-01", "group_by": "month"}),
    ("DataAnalysisTool", {"date_from": "2020-01-01", "date_to": "2026-01-01", "group_by": "month"}),
    ("DataAnalysisTool", {"date_from": "2026-01-01", "date_to": "2026-02-01", "group_by": "sql"}),
    ("DataAnalysisTool", {"date_from": "not-date", "date_to": "2026-02-01", "group_by": "month"}),
    (
        "DataAnalysisTool",
        {"date_from": "2026-01-01", "date_to": "2026-02-01", "group_by": "month", "tenant_id": "other"},
    ),
]


@pytest.mark.parametrize("tool,args", INVALID, ids=[f"invalid-{i + 1:02}" for i in range(len(INVALID))])
def test_invalid_schema_is_rejected_before_starting_a_worker(world, tool, args, monkeypatch):
    async def forbidden(*a, **kw):
        pytest.fail("Invalid tool parameters must not launch a worker")

    monkeypatch.setattr("apps.agents.services.asyncio.create_subprocess_exec", forbidden)
    with pytest.raises(ValidationError):
        execute(world["ctx"], tool, args)
    call = ToolCall.objects.get()
    assert call.status == "rejected" and call.args_redacted == {}


def test_duplicate_tool_calls_stop_loop_and_run_kind_is_in_idempotency(world, ingest, monkeypatch):
    ingest()
    ctx, run = start(world, "退款")

    async def repeated(*a, **kw):
        n = await sync_to_async(lambda: run.toolcall_set.count())()
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-{n}",
                    "type": "function",
                    "function": {"name": "KnowledgeSearchTool", "arguments": '{"query":"退款"}'},
                }
            ],
        }, {"source": "unknown"}

    monkeypatch.setattr("apps.agents.runner.decide", repeated)
    async_to_sync(consume)(AgentSession(ctx, run))
    run.refresh_from_db()
    assert run.error_code == "AGENT_TOOL_LOOP" and run.toolcall_set.filter(status="completed").count() == 1
    from apps.conversations.services import Conflict

    with pytest.raises(Conflict):
        begin_run(ctx, run.conversation_id, "退款", "agent", kind="rag")


def test_hard_timeout_kills_real_process_before_it_can_finish_late(world, settings, monkeypatch, tmp_path):
    real_spawn = asyncio.create_subprocess_exec
    children = []
    marker = tmp_path / "should-not-exist.txt"

    async def spawn(*args, **kwargs):
        process = await real_spawn(
            sys.executable,
            "-c",
            f"import time;from pathlib import Path;time.sleep(1.1);Path({str(marker)!r}).write_text('late')",
            **kwargs,
        )
        children.append(process)
        return process

    monkeypatch.setattr("apps.agents.services.asyncio.create_subprocess_exec", spawn)
    settings.AGENT_TOOL_TIMEOUT_SECONDS = 0.3
    started = time.monotonic()
    with pytest.raises(GatewayError, match="TOOL_TIMEOUT"):
        execute(world["ctx"], "KnowledgeSearchTool", {"query": "退款"})
    assert time.monotonic() - started < 1.0 and children[0].returncode is not None
    time.sleep(1.2)
    assert not marker.exists() and ToolCall.objects.get().status == "timed_out"


def test_total_deadline_cancels_pending_model(world, settings, monkeypatch):
    settings.AGENT_TOTAL_TIMEOUT_SECONDS = 0.3
    ctx, run = start(world, "长时间规划")
    closed = []

    async def slow(*a, **kw):
        try:
            await asyncio.sleep(10)
        finally:
            closed.append(time.monotonic())

    monkeypatch.setattr("apps.agents.runner.decide", slow)
    started = time.monotonic()
    async_to_sync(consume)(AgentSession(ctx, run))
    run.refresh_from_db()
    assert closed and run.error_code == "AGENT_TIMEOUT"
    assert closed[0] - started < 1.0  # Actual upstream work is stopped at the deadline.
    assert time.monotonic() - started < 3.5  # Allow bounded audit/connection cleanup on Windows.


def test_owner_cancel_closes_pending_planner_and_disconnect_at_meta_is_terminal(world, monkeypatch):
    ctx, run = start(world, "取消任务")
    closed = []

    async def slow(*a, **kw):
        try:
            await asyncio.sleep(10)
        finally:
            closed.append(True)

    monkeypatch.setattr("apps.agents.runner.decide", slow)

    async def cancel():
        iterator = AgentSession(ctx, run).stream()
        await anext(iterator)
        await asyncio.sleep(0.05)
        await sync_to_async(cancel_run)(ctx, run.pk)
        await consume_existing(iterator)

    async def consume_existing(iterator):
        return [item async for item in iterator]

    async_to_sync(cancel)()
    run.refresh_from_db()
    assert closed and run.status == "cancelled"
    ctx2, run2 = start(world, "立即断开", "disconnect")

    async def disconnect():
        iterator = AgentSession(ctx2, run2).stream()
        await anext(iterator)
        await iterator.aclose()

    async_to_sync(disconnect)()
    run2.refresh_from_db()
    assert run2.status == "interrupted"


def test_agent_sse_idempotency_and_cross_tenant_access(world, ingest):
    ingest()
    c = Conversation.objects.create(tenant=world["tenant"], user=world["admin"])
    token = str(RefreshToken.for_user(world["admin"]).access_token)

    async def request():
        client = AsyncClient()
        url = f"/api/v1/conversations/{c.pk}/agent:stream"
        kwargs = {
            "data": json.dumps({"question": "退款"}),
            "content_type": "application/json",
            "headers": {"Authorization": "Bearer " + token, "Idempotency-Key": "api-agent"},
        }
        response = await client.post(url, **kwargs)
        assert response.status_code == 200
        wire = b"".join([item async for item in response.streaming_content]).decode()
        assert "event: tool" in wire and "event: done" in wire
        replay = await client.post(url, **kwargs)
        assert replay.json()["replayed"]

    async_to_sync(request)()
    assert c.messages.count() == 2


def test_tool_call_actor_and_run_kind_are_database_guarded(world):
    ctx, run = start(world, "约束")
    with pytest.raises(IntegrityError), transaction.atomic():
        ToolCall.objects.create(
            tenant=world["tenant"],
            actor=world["member"],
            run=run,
            tool_name="KnowledgeSearchTool",
            trace_id="forged",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        type(run).objects.filter(pk=run.pk).update(kind="rag")


def test_fifth_tool_attempt_never_executes(world, ingest, monkeypatch):
    ingest()
    ctx, run = start(world, "有限调用")
    count = []

    async def planning(*a, **kw):
        count.append(kw["allow_tools"])
        n = len(count)
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"step-{n}",
                    "type": "function",
                    "function": {
                        "name": "KnowledgeSearchTool",
                        "arguments": json.dumps({"query": f"退款 {n}"}),
                    },
                }
            ],
        }, {"source": "unknown"}

    monkeypatch.setattr("apps.agents.runner.decide", planning)
    async_to_sync(consume)(AgentSession(ctx, run))
    run.refresh_from_db()
    assert count == [True, True, True, True, False]
    assert run.error_code == "AGENT_STEP_LIMIT" and run.toolcall_set.filter(status="completed").count() == 4
    assert run.toolcall_set.filter(status="rejected").count() == 1


def test_per_run_token_budget_rejects_before_upstream(world, settings):
    settings.AGENT_TOKEN_BUDGET = 1024
    ctx, run = start(world, "没有可用Token预算")
    async_to_sync(consume)(AgentSession(ctx, run))
    run.refresh_from_db()
    assert run.error_code == "AGENT_TOKEN_BUDGET_EXCEEDED"
    assert not UsageReservation.objects.filter(run=run).exists()


def test_cancel_kills_actual_tool_process(world, monkeypatch):
    real_spawn = asyncio.create_subprocess_exec

    async def test():
        launched, children = asyncio.Event(), []

        async def spawn(*args, **kwargs):
            process = await real_spawn(sys.executable, "-c", "import time; time.sleep(20)", **kwargs)
            children.append(process)
            launched.set()
            return process

        monkeypatch.setattr("apps.agents.services.asyncio.create_subprocess_exec", spawn)
        task = asyncio.create_task(invoke_tool(world["ctx"], "KnowledgeSearchTool", {"query": "退款"}))
        await launched.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None

    async_to_sync(test)()
    assert ToolCall.objects.get().status == "cancelled"


def test_summary_is_limited_to_conversation_scope(world, ingest):
    from apps.conversations.models import ConversationKnowledgeBase

    doc, *_ = ingest(kb=world["private"])
    ctx, run = start(world, "不能越过会话范围")
    ConversationKnowledgeBase.objects.create(
        tenant=world["tenant"], conversation_id=run.conversation_id, knowledge_base=world["kb"]
    )
    run.config_snapshot["kb_ids"] = [str(world["kb"].pk)]
    run.save(update_fields=["config_snapshot"])
    with pytest.raises(GatewayError, match="TOOL_SCOPE_REJECTED"):
        async_to_sync(invoke_tool)(ctx, "DocumentSummaryTool", {"document_id": str(doc.pk)}, run=run, step=1)


def test_analysis_history_and_reference_hide_after_revocation(world, client_for):
    from django.db.models import F

    from apps.users.models import Tenant

    ctx, run = start(world, "统计2026-01-01至2026-12-31按类别分组的问题")
    async_to_sync(consume)(AgentSession(ctx, run))
    run.refresh_from_db()
    call = run.toolcall_set.get()
    assert run.status == "completed" and call.source_id == "S1"
    Tenant.objects.filter(pk=world["tenant"].pk).update(acl_epoch=F("acl_epoch") + 1)
    client = client_for(world["admin"])
    assert client.get(f"/api/v1/agents/references/{call.pk}").status_code == 404
    data = client.get(f"/api/v1/runs/{run.pk}").json()
    assert not data["references"] and not data["tool_calls"] and "已隐藏" in data["content"]
    assert history(ctx, run.conversation)[-1]["status"] == "redacted"


def test_revoke_during_final_model_response_discards_answer(world, ingest, monkeypatch):
    doc, *_ = ingest(kb=world["private"])
    from apps.users.models import Permission, Role, UserRole

    role = Role.objects.create(tenant=world["tenant"], name="agent", code="agent")
    permission, _ = Permission.objects.get_or_create(code="agent.execute")
    role.permissions.add(permission)
    UserRole.objects.create(tenant=world["tenant"], user=world["member"], role=role)
    replace_acl(
        world["ctx"], world["private"], [{"user_id": str(world["member"].pk), "access_level": "read"}]
    )
    ctx, run = start(world, "退款", user=world["member"])
    from apps.agents.planner import decide as original

    async def revoke(context, current, messages, **kwargs):
        if any(m["role"] == "tool" for m in messages):
            await sync_to_async(replace_acl)(world["ctx"], world["private"], [])
            return {"content": "不能泄露的回复[S1]", "tool_calls": []}, {"source": "unknown"}
        return await original(context, current, messages, **kwargs)

    monkeypatch.setattr("apps.agents.runner.decide", revoke)
    wire = async_to_sync(consume)(AgentSession(ctx, run))
    run.refresh_from_db()
    assert (
        run.status == "interrupted"
        and "不能泄露" not in wire
        and "不能泄露" not in run.output_message.content
    )
