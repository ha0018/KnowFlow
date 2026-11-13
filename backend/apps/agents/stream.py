from asgiref.sync import sync_to_async
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import APIException

from ai_engine.gateway.config import GatewayError
from apps.conversations.stream import prepare

from .runner import AgentSession


@csrf_exempt
async def agent_stream(request, conversation_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        ctx, run, created = await sync_to_async(prepare)(request, conversation_id, kind="agent")
    except APIException as exc:
        return JsonResponse(
            {"error": exc.detail, "trace_id": getattr(request, "trace_id", "")}, status=exc.status_code
        )
    except Http404:
        return JsonResponse({"error": "not found"}, status=404)
    except GatewayError as exc:
        return JsonResponse(
            {"error": exc.code, "trace_id": getattr(request, "trace_id", "")},
            status=400 if exc.code == "QUESTION_TOKEN_BUDGET_EXCEEDED" else 503,
        )
    if not created:
        return JsonResponse({"run_id": str(run.pk), "status": run.status, "replayed": True})
    response = StreamingHttpResponse(
        AgentSession(ctx, run).stream(), content_type="text/event-stream; charset=utf-8"
    )
    response["Cache-Control"], response["X-Accel-Buffering"] = "no-cache, no-transform", "no"
    return response
