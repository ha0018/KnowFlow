import re
import time

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import DatabaseError, connection

from ai_engine.gateway import embed, generate
from ai_engine.gateway.config import GatewayError, profile


def schema_dimensions():
    try:
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute(
                    "SELECT format_type(atttypid,atttypmod) FROM pg_attribute "
                    "WHERE attrelid='documents_documentchunk'::regclass AND attname='embedding'"
                )
                value = cursor.fetchone()[0]
            else:
                cursor.execute("PRAGMA table_info(documents_documentchunk)")
                value = next((row[2] for row in cursor.fetchall() if row[1] == "embedding"), "")
        match = re.fullmatch(r"vector\((\d+)\)", value)
        return int(match[1]) if match else None
    except DatabaseError:
        return None


def configuration_status():
    models = []
    for kind in ["generation", "embedding"]:
        try:
            models.append({**profile(kind).public(), "configured": True})
        except GatewayError as exc:
            models.append({"kind": kind, "configured": False, "error_code": exc.code})
    dimensions = schema_dimensions()
    return {
        "models": models,
        "embedding_profile": settings.EMBEDDING_PROFILE,
        "embedding_dimensions": settings.EMBEDDING_DIMENSIONS,
        "database_dimensions": dimensions,
        "schema_matches": dimensions == settings.EMBEDDING_DIMENSIONS,
        "budget": {
            "input": settings.GENERATION_INPUT_BUDGET,
            "output": settings.GENERATION_MAX_OUTPUT_TOKENS,
            "history": settings.GENERATION_HISTORY_BUDGET,
            "estimator": "utf8-upper-bound-v1",
        },
    }


def probe_model(ctx, kind):
    from .metering import QuotaExceeded

    started = time.monotonic()
    result = {
        "kind": kind,
        "embedding_profile": settings.EMBEDDING_PROFILE,
        "protocol_verified": False,
        "status": "running",
    }
    try:
        model = profile(kind)
        result.update(model.public())
        if kind == "embedding":
            if schema_dimensions() != settings.EMBEDDING_DIMENSIONS:
                raise GatewayError("EMBEDDING_SCHEMA_MISMATCH")
            vectors, usage = embed(["KnowFlow model connection probe"], ctx=ctx)
            result["dimensions"] = len(vectors[0])
        else:

            async def run():
                output, usage = "", {"source": "unknown"}
                async for item in generate(
                    "请根据证据确认服务已连接。",
                    [{"source_id": "S1", "content": "这是一条公开的 KnowFlow 连通性测试资料。"}],
                    ctx=ctx,
                ):
                    if "text" in item:
                        output += item["text"]
                    if "usage" in item:
                        usage = item["usage"]
                if not output.strip():
                    raise GatewayError("MODEL_EMPTY_RESPONSE")
                return usage

            usage = async_to_sync(run)()
        result.update(status="passed", usage=usage, protocol_verified=model.provider == "compatible")
    except GatewayError as exc:
        result.update(status="failed", error_code=exc.code)
    except QuotaExceeded:
        result.update(status="failed", error_code="QUOTA_EXCEEDED")
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result
