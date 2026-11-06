from django.conf import settings
from django.db import connection
from django.db.models import Count
from django.http import JsonResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.agents.models import ToolCall
from apps.conversations.models import Run
from apps.documents.models import DocumentTask
from apps.documents.selectors import documents
from apps.knowledge.selectors import knowledge_bases
from apps.users.permissions import require_capability
from common.context import RequestContext

from .models import ModelCallLog


def live(request):
    return JsonResponse({"status": "alive"})


def ready(request):
    dependencies = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        dependencies["database"] = "ok"
    except Exception:
        dependencies["database"] = "unavailable"
    if not settings.CELERY_TASK_ALWAYS_EAGER:
        try:
            import redis

            redis.Redis.from_url(
                settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2
            ).ping()
            dependencies["redis"] = "ok"
        except Exception:
            dependencies["redis"] = "unavailable"
    if settings.S3_ENABLED:
        try:
            from django.core.files.storage import default_storage

            default_storage.connection.meta.client.head_bucket(Bucket=default_storage.bucket_name)
            dependencies["object_storage"] = "ok"
        except Exception:
            dependencies["object_storage"] = "unavailable"
    healthy = all(value == "ok" for value in dependencies.values())
    return JsonResponse(
        {
            "status": "ready" if healthy else "unavailable",
            "dependencies": dependencies,
            "model": "demo" if settings.AI_PROVIDER == "demo" else "configured-unprobed",
        },
        status=200 if healthy else 503,
    )


class DashboardView(APIView):
    def get(self, request):
        ctx = RequestContext.from_request(request)
        data = {
            "knowledge_bases": knowledge_bases(ctx).count(),
            "documents": documents(ctx).count(),
            "my_runs": Run.objects.filter(user=ctx.user, tenant_id=ctx.tenant_id).count(),
            "mode": settings.AI_PROVIDER,
        }
        if ctx.user.is_tenant_admin:
            data["tasks"] = list(
                DocumentTask.objects.filter(tenant_id=ctx.tenant_id)
                .values("status")
                .annotate(count=Count("id"))
            )
            data["tool_calls"] = ToolCall.objects.filter(tenant_id=ctx.tenant_id).count()
        return Response(data)


class CallsView(APIView):
    def get(self, request):
        ctx = RequestContext.from_request(request)
        require_capability(ctx, "monitoring.read")
        return Response(
            list(
                ModelCallLog.objects.filter(tenant_id=ctx.tenant_id).values(
                    "id", "trace_id", "task_type", "model", "status", "usage", "created_at"
                )[:100]
            )
        )
