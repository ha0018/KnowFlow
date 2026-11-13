import asyncio
import json
import logging
import time

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import serializers
from rest_framework.exceptions import APIException

from ai_engine.gateway import generate
from ai_engine.gateway.config import GatewayError
from ai_engine.retrieval import search
from ai_engine.retrieval.options import RetrievalOptions
from common.auth import TenantJWTAuthentication
from common.context import RequestContext

from .services import (
    attach_sources,
    begin_run,
    model_history,
    prepare_generation,
    run_is_allowed,
    save_progress,
)

logger = logging.getLogger(__name__)


class StreamInput(serializers.Serializer):
    question = serializers.CharField(max_length=4000)
    parent_id = serializers.UUIDField(required=False)


def prepare(request, conversation_id, *, kind="rag"):
    auth = TenantJWTAuthentication().authenticate(request)
    if auth is None:
        from rest_framework.exceptions import NotAuthenticated

        raise NotAuthenticated()
    user, _ = auth
    ctx = RequestContext.for_user(user, getattr(request, "trace_id", None))
    if len(request.body) > 20_000:
        raise serializers.ValidationError("请求过大")
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise serializers.ValidationError("无效 JSON") from exc
    if kind == "agent" and (not isinstance(body, dict) or set(body) - {"question", "parent_id"}):
        raise serializers.ValidationError("Agent 请求包含未知字段")
    serializer = StreamInput(data=body)
    serializer.is_valid(raise_exception=True)
    key = request.headers.get("Idempotency-Key", "")
    if not key or len(key) > 100:
        raise serializers.ValidationError("需要长度为 1–100 的 Idempotency-Key")
    run, created = begin_run(
        ctx,
        conversation_id,
        serializer.validated_data["question"],
        key,
        serializer.validated_data.get("parent_id"),
        kind=kind,
    )
    return ctx, run, created


def event(name, data):
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_events(ctx, run):
    text, usage = "", {"source": "unknown"}
    upstream = None
    pending = None
    final = False
    started, saved = time.monotonic(), time.monotonic()
    try:
        yield event(
            "meta",
            {"run_id": str(run.pk), "message_id": str(run.output_message_id), "trace_id": ctx.trace_id},
        )
        async with asyncio.timeout(settings.AI_TIMEOUT_SECONDS):
            retrieval_options = RetrievalOptions(
                **{
                    k: v
                    for k, v in run.config_snapshot.get("retrieval_options", {}).items()
                    if k in RetrievalOptions.__dataclass_fields__
                }
            )
            allowed_history = (
                await sync_to_async(model_history)(ctx, run) if retrieval_options.rewrite else ()
            )
            diagnostics = {}
            sources = await sync_to_async(search)(
                ctx,
                run.input_message.content,
                run.config_snapshot["kb_ids"] or None,
                run=run,
                options=retrieval_options,
                history=allowed_history,
                diagnostics=diagnostics,
            )
            run.config_snapshot["retrieval_diagnostics"] = diagnostics
            prepared = await sync_to_async(prepare_generation)(ctx, run, sources)
            sources = prepared.sources
            await sync_to_async(attach_sources)(ctx, run, sources)
            refs = await sync_to_async(
                lambda: list(run.output_message.references.values("id", "source_id"))
            )()
            ids = {r["source_id"]: str(r["id"]) for r in refs}
            yield event(
                "references",
                {
                    "sources": [
                        {
                            **{k: s[k] for k in ("source_id", "title", "version", "locator")},
                            "id": ids[s["source_id"]],
                        }
                        for s in sources
                    ]
                },
            )
            if not await sync_to_async(run_is_allowed)(ctx, run.pk):
                raise PermissionError("PERMISSION_CHANGED")

            async def before_request():
                if not await sync_to_async(run_is_allowed)(ctx, run.pk):
                    raise GatewayError("PERMISSION_CHANGED")

            upstream = generate(
                run.input_message.content,
                sources,
                prepared=prepared,
                ctx=ctx,
                run=run,
                before_request=before_request,
            )
            seq = 0
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(upstream))
                done, _ = await asyncio.wait([pending], timeout=1)
                if not await sync_to_async(run_is_allowed)(ctx, run.pk):
                    state = await sync_to_async(save_progress)(
                        run.pk, text, "interrupted", usage, "PERMISSION_CHANGED"
                    )
                    yield event("done", {"status": state, "usage": usage})
                    final = True
                    return
                if not done:
                    await sync_to_async(save_progress)(run.pk, text)
                    yield ": heartbeat\n\n"
                    continue
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    pending = None
                    break
                pending = None
                if "usage" in item:
                    usage = item["usage"]
                    continue
                seq += 1
                delta = item["text"]
                if len(text) + len(delta) > 16000:
                    raise ValueError("OUTPUT_LIMIT")
                text += delta
                if time.monotonic() - saved >= 1 or len(delta) > 1000:
                    await sync_to_async(save_progress)(run.pk, text)
                    saved = time.monotonic()
                yield event("delta", {"seq": seq, "text": delta})
            state = await sync_to_async(save_progress)(run.pk, text, "completed", usage)
            yield event(
                "done",
                {"status": state, "usage": usage, "duration_ms": round((time.monotonic() - started) * 1000)},
            )
            final = True
    except (asyncio.CancelledError, GeneratorExit):
        await asyncio.shield(
            sync_to_async(save_progress)(run.pk, text, "interrupted", usage, "CLIENT_DISCONNECTED")
        )
        final = True
        raise
    except Exception as exc:
        from apps.monitoring.metering import QuotaExceeded

        code = (
            exc.code
            if isinstance(exc, GatewayError)
            else "QUOTA_EXCEEDED"
            if isinstance(exc, QuotaExceeded)
            else "MODEL_TIMEOUT"
            if isinstance(exc, TimeoutError)
            else "GENERATION_FAILED"
        )
        logger.warning("stream_failed trace_id=%s code=%s", ctx.trace_id, code)
        terminal = "interrupted" if code == "PERMISSION_CHANGED" else "failed"
        state = await sync_to_async(save_progress)(run.pk, text, terminal, usage, code)
        if state == "failed":
            yield event("error", {"code": code, "trace_id": ctx.trace_id})
        yield event("done", {"status": state, "usage": usage})
        final = True
    finally:
        if pending is not None:
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass
        if upstream is not None:
            await upstream.aclose()
        if not final:
            await sync_to_async(save_progress)(run.pk, text, "interrupted", usage)


@csrf_exempt
async def messages_stream(request, conversation_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        ctx, run, created = await sync_to_async(prepare)(request, conversation_id)
    except APIException as exc:
        return JsonResponse({"error": exc.detail}, status=exc.status_code)
    except GatewayError as exc:
        return JsonResponse(
            {"error": exc.code, "trace_id": getattr(request, "trace_id", "")},
            status=400 if exc.code == "QUESTION_TOKEN_BUDGET_EXCEEDED" else 503,
        )
    except Exception as exc:
        from django.http import Http404

        if isinstance(exc, Http404):
            return JsonResponse({"error": "not found"}, status=404)
        raise
    if not created:
        return JsonResponse({"run_id": str(run.pk), "status": run.status, "replayed": True}, status=200)
    response = StreamingHttpResponse(stream_events(ctx, run), content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response
