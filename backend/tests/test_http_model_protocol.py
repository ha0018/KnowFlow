"""Real HTTP sockets against a deterministic protocol fixture, not an LLM.

This verifies transport/configuration; it must never be reported as vendor or
language-model quality validation.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.core.management import call_command

from ai_engine.gateway import embed
from ai_engine.gateway.config import GatewayError
from apps.monitoring.models import UsageReservation

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def servers(settings):
    active = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers.get("Authorization") != "Bearer " + self.server.test_key:
                self.send_error(401)
                return
            self.server.calls.append(self.path)
            if self.path.endswith("/embeddings"):
                if self.server.slow:
                    time.sleep(2)
                wire = json.dumps(
                    {
                        "data": [{"index": i, "embedding": [0.25] * 128} for i in range(len(body["input"]))],
                        "usage": {"prompt_tokens": 7, "total_tokens": 7},
                    }
                ).encode()
                content_type = "application/json"
            else:
                events = [
                    {"choices": [{"delta": {"content": "协议测试回答 [S1]"}, "finish_reason": "stop"}]},
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
                    },
                ]
                wire = (
                    "".join("data: " + json.dumps(v, ensure_ascii=False) + "\n\n" for v in events)
                    + "data: [DONE]\n\n"
                ).encode()
                content_type = "text/event-stream"
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    for key in ["generation-http-key", "embedding-http-key"]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.test_key, server.calls, server.slow = key, [], False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        active.append((server, thread))
    generation, embedding = [entry[0] for entry in active]
    settings.GENERATION_PROVIDER = "compatible"
    settings.GENERATION_BASE_URL = f"http://127.0.0.1:{generation.server_port}/v1"
    settings.GENERATION_API_KEY = generation.test_key
    settings.GENERATION_MODEL = "protocol-fixture-chat"
    settings.EMBEDDING_PROVIDER = "compatible"
    settings.EMBEDDING_BASE_URL = f"http://127.0.0.1:{embedding.server_port}/v1"
    settings.EMBEDDING_API_KEY = embedding.test_key
    settings.EMBEDDING_MODEL = "protocol-fixture-embedding"
    yield generation, embedding
    for server, thread in active:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_live_probe_over_real_http_is_metered_and_redacted(world, servers, tmp_path):
    output = tmp_path / "probe.json"
    call_command("probe_models", live=True, email=world["admin"].email, report=str(output))
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "passed" and all(p["protocol_verified"] for p in report["probes"])
    assert servers[0].calls == ["/v1/chat/completions"] and servers[1].calls == ["/v1/embeddings"]
    assert "generation-http-key" not in output.read_text(encoding="utf-8")
    assert set(UsageReservation.objects.values_list("charged_tokens", flat=True)) == {7, 28}


def test_real_http_embedding_deadline_is_bounded(world, servers, settings):
    servers[1].slow = True
    settings.EMBEDDING_TIMEOUT_SECONDS = 1
    settings.MODEL_MAX_RETRIES = 0
    started = time.monotonic()
    with pytest.raises(GatewayError, match="MODEL_TIMEOUT"):
        embed(["测试网络超时"], ctx=world["ctx"])
    assert time.monotonic() - started < 1.8
    row = UsageReservation.objects.get()
    assert row.status == "settled" and row.charged_tokens == row.reserved_tokens
