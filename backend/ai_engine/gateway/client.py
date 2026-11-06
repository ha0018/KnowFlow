import asyncio
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from asgiref.sync import async_to_sync
from django.conf import settings

from ai_engine.embeddings import demo_embed, validate_vectors

from .budget import estimate_tokens, prepare_input
from .config import GatewayError, profile


def normalize_usage(value, kind):
    if not isinstance(value, dict):
        return {"source": "unknown"}
    result = {
        key: value[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(value.get(key)) is int and 0 <= value[key] <= 1_000_000_000
    }
    if "prompt_tokens" in result:
        result["input_tokens"] = result["prompt_tokens"]
    if kind == "embedding":
        result["output_tokens"] = 0
        if "total_tokens" in result and "input_tokens" not in result:
            result["input_tokens"] = result["total_tokens"]
    elif "completion_tokens" in result:
        result["output_tokens"] = result["completion_tokens"]
    if "total_tokens" not in result and {"input_tokens", "output_tokens"} <= result.keys():
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return {"source": "provider" if "total_tokens" in result else "unknown", **result}


def mapped_error(exc):
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = (
            "MODEL_RATE_LIMITED"
            if status == 429
            else (
                "MODEL_AUTH_FAILED"
                if status in {401, 403}
                else "MODEL_UNAVAILABLE"
                if status >= 500
                else "MODEL_REQUEST_REJECTED"
            )
        )
        retry_after = None
        header = exc.response.headers.get("retry-after")
        if header:
            try:
                retry_after = max(0, float(header))
            except ValueError:
                try:
                    retry_after = max(
                        0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
                    )
                except (ValueError, TypeError, OverflowError):
                    pass
        return GatewayError(
            code, retryable=status in {408, 429, 500, 502, 503, 504}, retry_after=retry_after, status=status
        )
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return GatewayError("MODEL_TIMEOUT", retryable=True)
    if isinstance(exc, httpx.TransportError):
        return GatewayError("MODEL_CONNECTION_FAILED", retryable=True)
    return GatewayError("MODEL_PROTOCOL_INVALID")


def retry_delay(error, attempt, remaining):
    delay = error.retry_after if error.retry_after is not None else 0.2 * 2**attempt
    if (
        not error.retryable
        or attempt >= settings.MODEL_MAX_RETRIES
        or delay > settings.MODEL_RETRY_MAX_DELAY_SECONDS
        or delay >= remaining
    ):
        return None
    return delay


def new_meter(ctx, model, amount, output=0, run=None, task=None):
    from apps.monitoring.metering import CallMeter

    return CallMeter(ctx, model, amount, output, run=run, task=task)


async def embedding_request(model, body, remaining):
    # A total deadline also bounds a server that trickles response bytes slowly.
    async with asyncio.timeout(remaining):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(remaining, connect=min(5, remaining)), follow_redirects=False
        ) as client:
            response = await client.post(
                model.base_url.rstrip("/") + "/embeddings", headers=model.headers(), json=body
            )
            response.raise_for_status()
            return response.json()


def embed(texts, *, ctx=None, run=None, task=None, before_request=None):
    model = profile("embedding")
    if not texts or len(texts) > 128 or any(not isinstance(t, str) or not t.strip() for t in texts):
        raise GatewayError("EMBEDDING_INPUT_INVALID")
    costs = [estimate_tokens(t) for t in texts]
    if any(cost > settings.EMBEDDING_MAX_INPUT_TOKENS for cost in costs):
        raise GatewayError("EMBEDDING_INPUT_BUDGET_EXCEEDED")
    deadline = time.monotonic() + model.timeout
    for attempt in range(settings.MODEL_MAX_RETRIES + 1):
        meter = new_meter(ctx, model, sum(costs), run=run, task=task)
        meter.start()
        usage = {"source": "unknown"}
        try:
            if before_request:
                before_request()
            if model.provider == "demo":
                vectors = validate_vectors([demo_embed(text) for text in texts], len(texts))
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GatewayError("MODEL_TIMEOUT")
                meter.mark_sent()
                body = {"model": model.model, "input": texts, "encoding_format": "float"}
                if settings.EMBEDDING_SEND_DIMENSIONS:
                    body["dimensions"] = settings.EMBEDDING_DIMENSIONS
                data = async_to_sync(embedding_request)(model, body, remaining)
                rows = data["data"]
                if (
                    not isinstance(rows, list)
                    or any(type(item.get("index")) is not int for item in rows)
                    or sorted(item["index"] for item in rows) != list(range(len(texts)))
                ):
                    raise GatewayError("EMBEDDING_INDEX_INVALID")
                vectors = [item["embedding"] for item in sorted(rows, key=lambda item: item["index"])]
                try:
                    validate_vectors(vectors, len(texts))
                except (ValueError, TypeError):
                    raise GatewayError("EMBEDDING_VECTOR_INVALID") from None
                usage = normalize_usage(data.get("usage"), "embedding")
            meter.finish(usage)
            return vectors, usage
        except BaseException as exc:
            error = mapped_error(exc)
            meter.finish(usage, error=error)
            if not isinstance(exc, Exception):
                raise
            delay = retry_delay(error, attempt, deadline - time.monotonic())
            if delay is None:
                raise error from None
            time.sleep(delay)


async def generate(question, sources, *, history=(), prepared=None, ctx=None, run=None, before_request=None):
    model = profile("generation")
    prepared = prepared or prepare_input(question, sources, history)
    sources = prepared.sources
    if not sources:
        yield {"text": "当前可访问的资料中没有找到足够依据，暂时无法回答。"}
        yield {"usage": {"source": "unknown"}}
        return
    deadline = time.monotonic() + model.timeout
    for attempt in range(settings.MODEL_MAX_RETRIES + 1):
        meter = new_meter(
            ctx, model, prepared.input_tokens_estimate, settings.GENERATION_MAX_OUTPUT_TOKENS, run=run
        )
        await meter.astart()
        usage, output, emitted = {"source": "unknown"}, "", False
        try:
            if before_request:
                await before_request()
            if model.provider == "demo":
                output = "演示模式：以下为相关原文摘录，请结合上下文核对。\n\n" + "\n\n".join(
                    f"{s['content'][:900]} [{s['source_id']}]" for s in sources[:3]
                )
                output = output.encode()[: settings.GENERATION_MAX_OUTPUT_TOKENS].decode(errors="ignore")
                for start in range(0, len(output), 24):
                    emitted = True
                    yield {"text": output[start : start + 24]}
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GatewayError("MODEL_TIMEOUT")
                await meter.amark_sent()
                body = {
                    "model": model.model,
                    "messages": prepared.messages,
                    "stream": True,
                    settings.GENERATION_TOKEN_PARAMETER: settings.GENERATION_MAX_OUTPUT_TOKENS,
                    "stream_options": {"include_usage": True},
                }
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(remaining, connect=min(5, remaining)), follow_redirects=False
                ) as client:
                    manager = client.stream(
                        "POST",
                        model.base_url.rstrip("/") + "/chat/completions",
                        headers=model.headers(),
                        json=body,
                    )
                    response = await asyncio.wait_for(manager.__aenter__(), timeout=remaining)
                    try:
                        response.raise_for_status()
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            raise GatewayError("MODEL_STREAM_CONTENT_TYPE_INVALID")
                        lines, completed = response.aiter_lines(), False
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise GatewayError("MODEL_TIMEOUT")
                            try:
                                line = await asyncio.wait_for(anext(lines), timeout=remaining)
                            except StopAsyncIteration:
                                break
                            if len(line) > 65536:
                                raise GatewayError("MODEL_STREAM_EVENT_TOO_LARGE")
                            if not line.startswith("data:"):
                                continue
                            value = line[5:].strip()
                            if value == "[DONE]":
                                completed = True
                                break
                            data = json.loads(value)
                            if data.get("error"):
                                raise GatewayError("MODEL_STREAM_ERROR")
                            if data.get("usage"):
                                usage = normalize_usage(data["usage"], "generation")
                                yield {"usage": usage}
                            for choice in data.get("choices", []):
                                delta = choice.get("delta", {})
                                if delta.get("tool_calls") or delta.get("function_call"):
                                    raise GatewayError("MODEL_TOOL_CALL_NOT_ALLOWED")
                                text = delta.get("content")
                                if text:
                                    if not isinstance(text, str):
                                        raise GatewayError("MODEL_PROTOCOL_INVALID")
                                    output += text
                                    if estimate_tokens(output) > 64000:
                                        raise GatewayError("MODEL_OUTPUT_LIMIT")
                                    emitted = True
                                    yield {"text": text}
                                if choice.get("finish_reason"):
                                    if choice["finish_reason"] == "content_filter":
                                        raise GatewayError("MODEL_CONTENT_FILTERED")
                                    completed = True
                        if not completed:
                            raise GatewayError("MODEL_STREAM_INTERRUPTED")
                    finally:
                        await manager.__aexit__(None, None, None)
            if not output.strip():
                raise GatewayError("MODEL_EMPTY_RESPONSE")
            await meter.afinish(usage, output)
            yield {"usage": usage}
            return
        except BaseException as exc:
            error = (
                GatewayError("MODEL_CANCELLED")
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                else mapped_error(exc)
            )
            await asyncio.shield(meter.afinish(usage, output, error))
            if not isinstance(exc, Exception):
                raise
            delay = None if emitted else retry_delay(error, attempt, deadline - time.monotonic())
            if delay is None:
                raise error from None
            await asyncio.sleep(delay)
