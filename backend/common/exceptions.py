from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_handler

from ai_engine.gateway.config import GatewayError


def exception_handler(exc, context):
    if isinstance(exc, GatewayError):
        status = (
            429
            if exc.code == "MODEL_RATE_LIMITED"
            else (
                504
                if exc.code in {"MODEL_TIMEOUT", "TOOL_TIMEOUT", "AGENT_TIMEOUT"}
                else 400
                if exc.code
                in {
                    "EMBEDDING_INPUT_INVALID",
                    "EMBEDDING_INPUT_BUDGET_EXCEEDED",
                    "QUESTION_TOKEN_BUDGET_EXCEEDED",
                }
                else 503
            )
        )
        return Response(
            {"error": exc.code, "trace_id": getattr(context.get("request"), "trace_id", "")}, status=status
        )
    response = drf_handler(exc, context)
    if response is not None:
        response.data = {"error": response.data, "trace_id": getattr(context.get("request"), "trace_id", "")}
    return response
