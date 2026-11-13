import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from asgiref.sync import async_to_sync

from apps.agents.runner import AgentSession
from apps.conversations.models import Conversation
from apps.conversations.services import begin_run
from apps.monitoring.models import ModelCallLog, UsageReservation

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def planning_server(settings):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers.get("Authorization") == "Bearer agent-fixture-key"
            self.server.requests.append(body)
            if self.server.slow:
                time.sleep(2)
            observed = [m for m in body["messages"] if m["role"] == "tool"]
            if observed:
                source = json.loads(observed[-1]["content"])["sources"][0]
                message = {
                    "role": "assistant",
                    "content": source["content"] + " [" + source["source_id"] + "]",
                }
                reason = "stop"
            else:
                call = {
                    "id": "fixture-call-1",
                    "type": "function",
                    "function": {
                        "name": "KnowledgeSearchTool",
                        "arguments": json.dumps({"query": "退款", "kb_ids": None, "top_k": 5}),
                    },
                }
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [call] * (2 if self.server.parallel else 1),
                }
                reason = "tool_calls"
            wire = json.dumps(
                {
                    "choices": [{"message": message, "finish_reason": reason}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
                }
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests, server.parallel, server.slow = [], False, False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.GENERATION_PROVIDER = "compatible"
    settings.GENERATION_BASE_URL = f"http://127.0.0.1:{server.server_port}/v1"
    settings.GENERATION_API_KEY = "agent-fixture-key"
    settings.GENERATION_MODEL = "deterministic-tool-protocol-fixture"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def invoke(world):
    c = Conversation.objects.create(tenant=world["tenant"], user=world["admin"])
    run, _ = begin_run(world["ctx"], c.pk, "退款规定？", "http-agent", kind="agent")

    async def consume():
        return "".join([piece async for piece in AgentSession(world["ctx"], run).stream()])

    wire = async_to_sync(consume)()
    run.refresh_from_db()
    return run, wire


def test_real_http_function_call_contract_and_separate_metering(world, ingest, planning_server):
    ingest()
    ingest(text="不应发送的私有资料SUPER-PRIVATE", kb=world["private"])
    # Use a granted member, so private text cannot reach the planner.
    from apps.users.models import Permission, Role, UserRole
    from common.context import RequestContext

    role = Role.objects.create(tenant=world["tenant"], name="Agent", code="agent")
    permission, _ = Permission.objects.get_or_create(code="agent.execute")
    role.permissions.add(permission)
    UserRole.objects.create(tenant=world["tenant"], user=world["member"], role=role)
    world = {**world, "admin": world["member"], "ctx": RequestContext.for_user(world["member"])}
    run, wire = invoke(world)
    assert run.status == "completed", (run.error_code, wire)
    assert len(planning_server.requests) == 2
    body = planning_server.requests[0]
    assert body["parallel_tool_calls"] is False and body["stream"] is False
    assert {t["function"]["name"] for t in body["tools"]} == {
        "KnowledgeSearchTool",
        "DocumentSummaryTool",
        "DataAnalysisTool",
    }
    assert all(
        t["function"]["strict"] and t["function"]["parameters"]["additionalProperties"] is False
        for t in body["tools"]
    )
    assert "SUPER-PRIVATE" not in json.dumps(planning_server.requests)
    assert (
        ModelCallLog.objects.filter(run=run, task_type="agent_planner", usage__source="provider").count() == 2
    )
    assert not UsageReservation.objects.filter(run=run, status="reserved").exists()


def test_parallel_model_calls_are_rejected_before_execution(world, planning_server):
    planning_server.parallel = True
    run, _ = invoke(world)
    assert run.status == "failed" and run.error_code == "AGENT_PARALLEL_CALL_REJECTED"
    assert not run.toolcall_set.exists()
    assert UsageReservation.objects.get(run=run).charged_tokens == 17


def test_real_http_planner_deadline_closes_and_settles_unknown_usage(world, planning_server, settings):
    planning_server.slow = True
    settings.GENERATION_TIMEOUT_SECONDS = 1
    settings.MODEL_MAX_RETRIES = 0
    run, _ = invoke(world)
    assert run.status == "failed" and run.error_code == "MODEL_TIMEOUT"
    row = UsageReservation.objects.get(run=run)
    assert row.status == "settled" and row.usage["quota_basis"] == "reserved_unknown"


def test_total_timeout_below_model_timeout_cancels_native_http(world, planning_server, settings):
    planning_server.slow = True
    settings.AGENT_TOTAL_TIMEOUT_SECONDS = 0.5
    run, _ = invoke(world)
    assert run.status == "failed" and run.error_code == "AGENT_TIMEOUT"
    row = UsageReservation.objects.get(run=run)
    assert row.status != "reserved" and row.usage["error_code"] == "MODEL_CANCELLED"
