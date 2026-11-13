"""Private, one-request worker protocol. Only the parent launches this module."""

import json
import sys


def main():
    import django
    from django.conf import settings

    data = json.loads(sys.stdin.buffer.read(131072))
    settings.DATABASES = {"default": data["database"]}
    # Only the parent-provided application settings whitelist is accepted.
    prefixes = (
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
    for name, value in data["settings"].items():
        if name.startswith(prefixes):
            setattr(settings, name, value)
    django.setup()
    import hashlib
    from dataclasses import replace

    from django.core.serializers.json import DjangoJSONEncoder
    from django.db import connection, connections
    from django.http import Http404
    from rest_framework.exceptions import PermissionDenied, ValidationError

    from ai_engine.gateway.config import GatewayError
    from apps.monitoring.metering import QuotaExceeded
    from common.context import RequestContext

    from .models import ToolCall
    from .services import canonical
    from .tool_core import execute_core

    try:
        call = ToolCall.objects.select_related("actor__tenant", "run").get(
            pk=data["call_id"], status="running"
        )
        if (
            hashlib.sha256(canonical({"tool": call.tool_name, "args": data["args"]}).encode()).hexdigest()
            != call.arguments_hash
        ):
            raise GatewayError("TOOL_ARGUMENT_HASH_MISMATCH")
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    [str(max(1, int(settings.AGENT_TOOL_TIMEOUT_SECONDS * 1000)))],
                )
                cursor.execute("SET lock_timeout = '1s'")
            elif connection.vendor == "sqlite":
                cursor.execute("PRAGMA busy_timeout=1000")
        ctx = replace(
            RequestContext.for_user(call.actor, call.trace_id), acl_epoch=call.guard_snapshot["acl_epoch"]
        )
        result = execute_core(ctx, call.tool_name, data["args"], call.run)
        wire = json.dumps({"result": result}, cls=DjangoJSONEncoder, ensure_ascii=False).encode()
        if len(wire) > 65536:
            raise GatewayError("TOOL_RESULT_LIMIT")
    except Exception as exc:
        code = (
            "TOOL_NOT_ACCESSIBLE"
            if isinstance(exc, Http404)
            else "PERMISSION_CHANGED"
            if isinstance(exc, PermissionDenied)
            else "QUOTA_EXCEEDED"
            if isinstance(exc, QuotaExceeded)
            else "TOOL_ARGUMENT_INVALID"
            if isinstance(exc, ValidationError)
            else exc.code
            if isinstance(exc, GatewayError)
            else "TOOL_FAILED"
        )
        wire = json.dumps({"error": code}).encode()
    finally:
        connections.close_all()
    sys.stdout.buffer.write(wire)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
