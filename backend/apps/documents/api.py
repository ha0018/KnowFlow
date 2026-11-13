from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.knowledge.api import ACLEntrySerializer
from apps.knowledge.selectors import get_knowledge_base
from apps.knowledge.services import bump_epoch, replace_acl
from apps.users.models import Tenant
from common.context import RequestContext, audit, reload_context

from .models import Document, DocumentTask
from .selectors import documents, get_document
from .services import reindex, upload


class DocumentSerializer(serializers.ModelSerializer):
    can_manage = serializers.SerializerMethodField()

    def get_can_manage(self, obj):
        if "manage_ids" not in self.context:
            request = self.context.get("request")
            self.context["manage_ids"] = (
                set(documents(RequestContext.from_request(request), "manage").values_list("id", flat=True))
                if request
                else set()
            )
        return obj.pk in self.context["manage_ids"]

    status = serializers.CharField(source="desired_build.status", default="PENDING", read_only=True)
    active_version = serializers.IntegerField(
        source="active_build.version.version_no", default=None, read_only=True
    )
    error_code = serializers.CharField(source="desired_build.error_code", default="", read_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "knowledge_base_id",
            "title",
            "acl_mode",
            "status",
            "active_version",
            "error_code",
            "active_build_id",
            "desired_build_id",
            "created_at",
            "can_manage",
        ]
        read_only_fields = ["id", "knowledge_base_id", "active_build_id", "desired_build_id", "created_at"]


class UploadInput(serializers.Serializer):
    file = serializers.FileField(allow_empty_file=True)
    title = serializers.CharField(max_length=240, required=False)
    document_id = serializers.UUIDField(required=False)


class UploadView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, kb_id):
        ctx = RequestContext.from_request(request)
        get_knowledge_base(ctx, kb_id)
        qs = (
            documents(ctx)
            .filter(knowledge_base_id=kb_id)
            .select_related("desired_build", "active_build__version")
        )
        from rest_framework.pagination import PageNumberPagination

        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(
            DocumentSerializer(page, many=True, context={"request": request}).data
        )

    def post(self, request, kb_id):
        serializer = UploadInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        doc, version, build, task = upload(
            RequestContext.from_request(request),
            kb_id,
            uploaded=serializer.validated_data["file"],
            title=serializer.validated_data.get("title"),
            document_id=serializer.validated_data.get("document_id"),
        )
        return Response(
            {"document_id": doc.pk, "version_id": version.pk, "build_id": build.pk, "task_id": task.pk},
            status=202,
        )


class DocumentViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DocumentSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        return documents(RequestContext.from_request(self.request)).select_related(
            "desired_build", "active_build__version"
        )

    @action(detail=True, methods=["post"])
    def reindex(self, request, pk=None):
        build, task = reindex(RequestContext.from_request(request), pk)
        return Response({"build_id": build.pk, "task_id": task.pk}, status=202)

    @action(detail=True, methods=["get"])
    def versions(self, request, pk=None):
        doc = self.get_object()
        return Response(
            [
                {
                    "id": v.pk,
                    "version_no": v.version_no,
                    "sha256": v.sha256,
                    "original_name": v.original_name,
                    "size_bytes": v.size_bytes,
                    "created_at": v.created_at,
                }
                for v in doc.versions.order_by("-version_no")
            ]
        )

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        ctx = RequestContext.from_request(request)
        doc = get_document(ctx, pk)
        version = (
            doc.versions.filter(pk=request.query_params["version_id"]).first()
            if request.query_params.get("version_id")
            else (
                doc.active_build.version
                if doc.active_build_id
                else doc.versions.order_by("-version_no").first()
            )
        )
        if version is None:
            raise ValidationError("版本不存在")
        audit(ctx, "document.download", doc)
        return FileResponse(
            version.file.open("rb"),
            as_attachment=True,
            filename=version.original_name,
            content_type=version.mime_type,
        )

    @action(detail=True, methods=["get", "post"], url_path="access")
    def access(self, request, pk=None):
        ctx = RequestContext.from_request(request)
        doc = get_document(ctx, pk, "manage")
        if request.method == "GET":
            return Response(
                {"acl_mode": doc.acl_mode, "entries": ACLEntrySerializer(doc.acl.all(), many=True).data}
            )
        mode = serializers.ChoiceField(choices=["inherit", "restricted"]).run_validation(
            request.data.get("acl_mode")
        )
        entries = ACLEntrySerializer(data=request.data.get("entries", []), many=True)
        entries.is_valid(raise_exception=True)
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=ctx.tenant_id)
            ctx = reload_context(ctx)
            doc = get_document(ctx, pk, "manage")
            replace_acl(ctx, doc, entries.validated_data)
            doc.acl_mode = mode
            doc.save(update_fields=["acl_mode"])
        return Response({"acl_mode": doc.acl_mode})

    def destroy(self, request, pk=None):
        ctx = RequestContext.from_request(request)
        doc = get_document(ctx, pk, "edit")
        with transaction.atomic():
            doc.deleted_at = timezone.now()
            doc.save(update_fields=["deleted_at"])
            bump_epoch(ctx)
            audit(ctx, "document.delete", doc)
        return Response(status=204)


class TaskView(APIView):
    def get(self, request, task_id):
        ctx = RequestContext.from_request(request)
        task = get_object_or_404(
            DocumentTask.objects.select_related("build__version"),
            pk=task_id,
            tenant_id=ctx.tenant_id,
            build__version__document_id__in=documents(ctx).values("id"),
        )
        return Response(
            {
                "id": task.pk,
                "status": task.status,
                "stage": task.stage,
                "attempt": task.attempt,
                "error_code": task.error_code,
                "chunk_count": task.build.chunks.count(),
                "trace_id": task.trace_id,
            }
        )
