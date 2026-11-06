import json

import httpx
import pytest
from asgiref.sync import async_to_sync

from ai_engine.gateway import embed, generate


def test_embedding_protocol_preserves_order_and_provider_usage(settings, monkeypatch):
    settings.EMBEDDING_PROVIDER = "compatible"
    settings.AI_API_KEY = "test-key"
    original_client = httpx.Client

    def handle(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content)["input"] == ["第一条", "第二条"]
        return httpx.Response(
            200,
            json={
                "data": [{"index": 1, "embedding": [0.5] * 128}, {"index": 0, "embedding": [1.0] * 128}],
                "usage": {"total_tokens": 12},
            },
        )

    monkeypatch.setattr(
        "ai_engine.gateway.httpx.Client",
        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw),
    )
    vectors, usage = embed(["第一条", "第二条"])
    assert vectors[0][0] == 1.0 and vectors[1][0] == 0.5
    assert usage == {"source": "provider", "total_tokens": 12}


@pytest.mark.parametrize("bad_vector", [[0.0] * 128, [1.0] * 127, [float("nan")] * 128])
def test_invalid_vectors_never_publish(bad_vector):
    from ai_engine.embeddings import validate_vectors

    with pytest.raises(ValueError):
        validate_vectors([bad_vector], 1)


def test_model_sse_protocol_and_prompt_boundaries(settings, monkeypatch):
    settings.AI_PROVIDER = "compatible"
    settings.AI_API_KEY = "test-key"
    settings.AI_MODEL = "test-model"
    original_client = httpx.AsyncClient

    def handle(request):
        payload = json.loads(request.content)
        assert payload["stream"] and len(payload["messages"]) == 2
        assert "非可信" in payload["messages"][0]["content"]
        data = [
            {"choices": [{"delta": {"content": "有依据的回答 [S1]"}}]},
            {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 8}},
        ]
        wire = (
            "".join("data: " + json.dumps(d, ensure_ascii=False) + "\n\n" for d in data) + "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=wire, headers={"Content-Type": "text/event-stream"})

    monkeypatch.setattr(
        "ai_engine.gateway.httpx.AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw),
    )

    async def collect():
        return [item async for item in generate("问题", [{"source_id": "S1", "content": "原文"}])]

    result = async_to_sync(collect)()
    assert result[0] == {"text": "有依据的回答 [S1]"}
    assert result[1]["usage"]["source"] == "provider"
