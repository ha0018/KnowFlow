"""Tool admission/audit and killable execution. No model-supplied identity is used."""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import replace

from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from ai_engine.gateway.config import GatewayError
from apps.monitoring.metering import QuotaExceeded, settle
from apps.monitoring.models import UsageReservation
from apps.users.models import Tenant
from common.context import audit

from .models import ToolCall
from .policy import fresh_context, limits
from .schemas import redacted, validate_args

SETTING_PREFIXES = (
    "AI_",
    "GENERATION_",
    "EMBEDDING_",
    "RETRIEVAL_",
    "RERANKER_",
    "REWRITE_",
    "MODEL_",
    "DEFAULT_",
    "AGENT_",
)


def canonical(value):
    return json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@transaction.atomic
def begin_call(ctx, name, args, run=None, step=0):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    ctx = fresh_context(ctx, run)
    error, params = None, None
    try:
        params = validate_args(name, args)
    except ValidationError as exc:
        error = exc
    digest = hashlib.sha256(
        canonical({"tool": name, "args": params if params is not None else args}).encode()
    ).hexdigest()
    if run is not None and error is None:
        maximum = run.config_snapshot["agent"]["max_tools"]
        if not 1 <= step <= maximum:
            error = GatewayError("AGENT_STEP_LIMIT")
        elif ToolCall.objects.filter(
            run=run, arguments_hash=digest, status__in=["running", "completed"]
        ).exists():
            error = GatewayError("AGENT_TOOL_LOOP")
    call_id = uuid.uuid4()
    call = ToolCall.objects.create(
        id=call_id,
        tenant_id=ctx.tenant_id,
        actor=ctx.user,
        run=run,
        tool_name=name[:80] if isinstance(name, str) else "<invalid>",
        schema_version="agent-tools-v2",
        step_index=step,
        trace_id=f"{ctx.trace_id[:24]}:tool:{call_id.hex[:16]}",
        arguments_hash=digest,
        args_redacted=redacted(params) if params is not None else {},
        guard_snapshot={"acl_epoch": ctx.acl_epoch, "run_id": str(run.pk) if run else None},
        status="rejected" if error else "running",
        error_code=getattr(error, "code", "TOOL_ARGUMENT_INVALID") if error else "",
        finished_at=timezone.now() if error else None,
    )
    return call, json.loads(canonical(params)) if params is not None else None, error


@transaction.atomic
def finish_call(ctx, call_id, result=None, *, status="completed", code="", duration=0):
    Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
    call = (
        ToolCall.objects.select_for_update()
        .select_related("run")
        .get(pk=call_id, tenant_id=ctx.tenant_id, actor=ctx.user)
    )
    if call.status != "running":
        return call
    if status == "completed":
        fresh_context(ctx, call.run, (result or {}).get("sources", ()))
        call.guard_snapshot = {
            **call.guard_snapshot,
            "chunk_ids": [s["chunk_id"] for s in (result or {}).get("sources", ())],
        }
        call.result_summary = {
            "keys": list(result or {}),
            "source_count": len((result or {}).get("sources", [])),
        }
        if call.tool_name == "DataAnalysisTool":
            call.result_summary["analysis"] = result
    call.status, call.error_code = status, code
    call.finished_at, call.latency_ms = timezone.now(), max(0, int(duration * 1000))
    call.save(
        update_fields=[
            "status",
            "error_code",
            "finished_at",
            "latency_ms",
            "result_summary",
            "guard_snapshot",
        ]
    )
    audit(
        replace(ctx, trace_id=call.trace_id), "tool.execute" if status == "completed" else "tool.stop", call
    )
    return call


def worker_payload(call, params):
    values = {key: getattr(settings, key) for key in dir(settings) if key.startswith(SETTING_PREFIXES)}
    return canonical(
        {"call_id": str(call.pk), "args": params, "settings": values, "database": connection.settings_dict}
    ).encode()


def cleanup_reservations(trace_id):
    for pk in UsageReservation.objects.filter(trace_id=trace_id, status="reserved").values_list(
        "pk", flat=True
    ):
        settle(pk, sent=None, status="interrupted")


async def invoke_tool(ctx, name, args, *, run=None, step=0, deadline=None):
    started = time.monotonic()
    call, params, error = await sync_to_async(begin_call)(ctx, name, args, run, step)
    if error:
        raise error
    ctx = replace(ctx, trace_id=call.trace_id)
    process, launch = None, None
    try:
        timeout = (run.config_snapshot["agent"] if run else limits())["tool_seconds"]
        expires = min(started + timeout, deadline if deadline is not None else float("inf"))
        if expires <= time.monotonic():
            raise GatewayError("TOOL_TIMEOUT")
        env = dict(
            os.environ,
            DJANGO_SETTINGS_MODULE=str(settings.SETTINGS_MODULE or "config.settings.dev"),
            PYTHONIOENCODING="utf-8",
        )
        payload = await sync_to_async(worker_payload)(call, params)
        # A subprocess has no arbitrary command/tool/import name from the request.
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "apps.agents.worker",
                cwd=str(settings.BASE_DIR),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        process = await asyncio.shield(launch)
        try:
            async with asyncio.timeout_at(expires):
                wire, _ = await process.communicate(payload)
        except TimeoutError:
            raise GatewayError("TOOL_TIMEOUT") from None
        if process.returncode != 0 or len(wire) > 65536:
            raise GatewayError("TOOL_WORKER_FAILED")
        try:
            message = json.loads(wire)
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("TOOL_PROTOCOL_INVALID") from None
        if "error" in message:
            code = message["error"]
            if code == "TOOL_NOT_ACCESSIBLE":
                raise Http404()
            if code == "QUOTA_EXCEEDED":
                raise QuotaExceeded()
            raise GatewayError(
                code if isinstance(code, str) and code.isupper() and len(code) <= 80 else "TOOL_FAILED"
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise GatewayError("TOOL_PROTOCOL_INVALID")
        call = await sync_to_async(finish_call)(ctx, call.pk, result, duration=time.monotonic() - started)
        return result, call
    except BaseException as exc:
        # Terminate work before database audit/settlement, which may itself wait.
        if process is None and launch is not None:
            try:
                process = await asyncio.shield(launch)
            except (asyncio.CancelledError, Exception):
                pass
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.shield(process.wait())
        code = (
            "AGENT_CANCELLED"
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
            else "TOOL_NOT_ACCESSIBLE"
            if isinstance(exc, Http404)
            else "PERMISSION_CHANGED"
            if isinstance(exc, PermissionDenied)
            else "QUOTA_EXCEEDED"
            if isinstance(exc, QuotaExceeded)
            else exc.code
            if isinstance(exc, GatewayError)
            else "TOOL_FAILED"
        )
        status = (
            "cancelled" if code == "AGENT_CANCELLED" else "timed_out" if code == "TOOL_TIMEOUT" else "failed"
        )
        await asyncio.shield(
            sync_to_async(finish_call)(
                ctx, call.pk, status=status, code=code, duration=time.monotonic() - started
            )
        )
        if isinstance(
            exc, (Http404, PermissionDenied, ValidationError, QuotaExceeded, GatewayError)
        ) or not isinstance(exc, Exception):
            raise
        raise GatewayError("TOOL_WORKER_FAILED") from None
    finally:
        if process is None and launch is not None:
            # Cancellation during launch must not orphan a process.
            try:
                process = await asyncio.shield(launch)
            except (asyncio.CancelledError, Exception):
                pass
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.shield(process.wait())
        await asyncio.shield(sync_to_async(cleanup_reservations)(call.trace_id))


def execute(ctx, name, args):
    return async_to_sync(invoke_tool)(ctx, name, args)[0]
