import asyncio
import json
import re
import time

from asgiref.sync import sync_to_async
from django.db import transaction
from django.http import Http404
from rest_framework.exceptions import PermissionDenied, ValidationError

from ai_engine.gateway.config import GatewayError
from apps.conversations.services import reference_data, save_progress
from apps.monitoring.metering import QuotaExceeded
from apps.monitoring.models import UsageReservation
from apps.users.models import Tenant

from .evidence import analysis_references, observe, wire_size
from .models import ToolCall
from .planner import decide, initial_messages
from .policy import fresh_context
from .services import invoke_tool


def wire(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def citations(ctx, run):
    refs = [
        reference_data(r)
        for r in run.output_message.references.select_related("chunk__document", "chunk__build__version")
    ]
    return sorted([*refs, *analysis_references(ctx, run)], key=lambda r: int(r["source_id"][1:]))


def guard(ctx, run):
    sources = list(run.output_message.references.values("chunk_id"))
    return fresh_context(ctx, run, sources)


def usage_summary(run):
    rows = list(UsageReservation.objects.filter(run=run).values("usage", "charged_tokens", "status"))
    known = bool(rows) and all(r["usage"].get("source") == "provider" for r in rows)
    return {
        "source": "provider" if known else "unknown",
        "total_tokens": sum(r["usage"].get("total_tokens", 0) for r in rows) if known else None,
        "quota_tokens": sum(r["charged_tokens"] for r in rows),
        "calls": len(rows),
    }


@transaction.atomic
def complete(ctx, run, text, usage):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    guard(ctx, run)
    refs = citations(ctx, run)
    state = save_progress(run.pk, text, "completed", usage)
    return state, refs


class AgentSession:
    def __init__(self, ctx, run):
        self.ctx, self.run = ctx, run
        self.events = asyncio.Queue(maxsize=600)
        self.text, self.stop_code = "", ""
        self.task = None
        self.finalizing = False

    def emit(self, event, data):
        try:
            self.events.put_nowait(wire(event, data))
        except asyncio.QueueFull:
            raise GatewayError("AGENT_CLIENT_TOO_SLOW") from None

    async def check(self):
        await sync_to_async(guard)(self.ctx, self.run)

    async def watch(self):
        while self.task and not self.task.done():
            await asyncio.sleep(0.2)
            if self.finalizing:
                return
            try:
                await self.check()
            except Exception as exc:
                self.stop_code = exc.code if isinstance(exc, GatewayError) else "PERMISSION_CHANGED"
                self.task.cancel()
                return

    async def produce(self):
        started = time.monotonic()
        deadline = started + self.run.config_snapshot["agent"]["total_seconds"]
        budget = self.run.config_snapshot["agent"]["evidence_bytes"]
        maximum = self.run.config_snapshot["agent"]["max_tools"]
        messages, calls_seen = initial_messages(self.run.input_message.content), set()
        status, code = "completed", ""
        try:
            async with asyncio.timeout_at(deadline):
                for step in range(1, maximum + 2):
                    await self.check()
                    response, _ = await decide(
                        self.ctx,
                        self.run,
                        messages,
                        allow_tools=step <= maximum,
                        deadline=deadline,
                        guard=self.check,
                    )
                    await self.check()
                    calls = response.get("tool_calls") or []
                    if not calls:
                        refs = await sync_to_async(citations)(self.ctx, self.run)
                        self.text = response.get("content") or ""
                        if not refs:
                            self.text = "当前可访问资料没有足够依据，暂时无法回答。"
                        else:
                            ids = {ref["source_id"] for ref in refs}
                            cited = set(re.findall(r"\[(S\d+)\]", self.text))
                            if not cited or not cited <= ids:
                                self.text = ""
                                raise GatewayError("AGENT_CITATION_INVALID")
                        if len(self.text.encode()) > self.run.config_snapshot["agent"]["output_bytes"]:
                            self.text = ""
                            raise GatewayError("AGENT_OUTPUT_LIMIT")
                        break
                    call = calls[0]
                    if call["id"] in calls_seen:
                        raise GatewayError("AGENT_CALL_ID_REUSED")
                    calls_seen.add(call["id"])
                    function = call["function"]
                    self.emit("tool", {"step": step, "name": function["name"], "status": "running"})
                    result, audit_call = await invoke_tool(
                        self.ctx,
                        function["name"],
                        function["arguments"],
                        run=self.run,
                        step=step,
                        deadline=deadline,
                    )
                    await self.check()
                    observation = await sync_to_async(observe)(self.ctx, self.run, audit_call, result, budget)
                    budget -= wire_size(observation)
                    messages.append(response)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(observation, ensure_ascii=False),
                        }
                    )
                    self.emit(
                        "tool",
                        {
                            "step": step,
                            "name": function["name"],
                            "status": "completed",
                            "call_id": str(audit_call.pk),
                            "duration_ms": audit_call.latency_ms,
                        },
                    )
                    await sync_to_async(save_progress)(self.run.pk, "")
        except BaseException as exc:
            code = (
                self.stop_code or "CLIENT_DISCONNECTED"
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                else "AGENT_TIMEOUT"
                if isinstance(exc, TimeoutError)
                else "TOOL_ARGUMENT_INVALID"
                if isinstance(exc, ValidationError)
                else "TOOL_NOT_ACCESSIBLE"
                if isinstance(exc, Http404)
                else "PERMISSION_CHANGED"
                if isinstance(exc, PermissionDenied)
                else "QUOTA_EXCEEDED"
                if isinstance(exc, QuotaExceeded)
                else exc.code
                if isinstance(exc, GatewayError)
                else "AGENT_FAILED"
            )
            status = (
                "interrupted"
                if code
                in {"CLIENT_DISCONNECTED", "PERMISSION_CHANGED", "AGENT_RUN_STOPPED", "AGENT_CANCELLED"}
                else "failed"
            )
            if not self.text and status == "failed":
                completed = await sync_to_async(
                    lambda: ToolCall.objects.filter(run=self.run, status="completed").count()
                )()
                self.text = f"任务未完成，已完成 {completed} 次只读工具调用。请查看执行记录和停止原因后重试。"
        finally:
            self.finalizing = True
            # Persist exactly one terminal; a prior owner cancellation wins.
            usage = await asyncio.shield(sync_to_async(usage_summary)(self.run))
            refs = []
            if status == "completed":
                try:
                    state, refs = await asyncio.shield(
                        sync_to_async(complete)(self.ctx, self.run, self.text, usage)
                    )
                except Exception:
                    self.text, status, code = "", "interrupted", "PERMISSION_CHANGED"
                    state = await asyncio.shield(
                        sync_to_async(save_progress)(self.run.pk, "", status, usage, code)
                    )
            else:
                state = await asyncio.shield(
                    sync_to_async(save_progress)(self.run.pk, self.text, status, usage, code)
                )
            try:
                if state == "completed":
                    self.emit("references", {"sources": refs})
                    for number, start in enumerate(range(0, len(self.text), 40), 1):
                        self.emit("delta", {"seq": number, "text": self.text[start : start + 40]})
                if code and state == "failed":
                    self.emit("error", {"code": code, "trace_id": self.ctx.trace_id})
                self.emit(
                    "done",
                    {
                        "status": state,
                        "usage": usage,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
            except GatewayError:
                pass

    async def stream(self):
        self.task = asyncio.create_task(self.produce())
        watcher = asyncio.create_task(self.watch())
        try:
            yield wire(
                "meta",
                {
                    "run_id": str(self.run.pk),
                    "message_id": str(self.run.output_message_id),
                    "trace_id": self.ctx.trace_id,
                    "kind": "agent",
                    "provider": self.run.config_snapshot["provider"],
                    "limits": self.run.config_snapshot["agent"],
                },
            )
            while True:
                try:
                    item = await asyncio.wait_for(self.events.get(), timeout=1)
                except TimeoutError:
                    if self.task.done():
                        break
                    await sync_to_async(save_progress)(self.run.pk, self.text)
                    yield ": heartbeat\n\n"
                    continue
                yield item
                if item.startswith("event: done"):
                    break
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            if not self.task.done() and not self.task.cancelling():
                self.stop_code = self.stop_code or "CLIENT_DISCONNECTED"
                self.task.cancel()
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                await asyncio.shield(
                    sync_to_async(save_progress)(
                        self.run.pk, self.text, "interrupted", error=self.stop_code or "CLIENT_DISCONNECTED"
                    )
                )
