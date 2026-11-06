from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.users.permissions import require_capability
from common.context import RequestContext, audit

from .models import KnowledgeBase
from .selectors import get_knowledge_base, knowledge_bases
from .services import bump_epoch, replace_acl


class KnowledgeBaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeBase
        fields = ["id", "name", "description", "visibility", "owner_id", "created_at"]
        read_only_fields = ["id", "owner_id", "created_at"]


class ACLEntrySerializer(serializers.Serializer):
    user_id = serializers.UUIDField(required=False, allow_null=True)
    role_id = serializers.UUIDField(required=False, allow_null=True)
    access_level = serializers.ChoiceField(choices=["read", "edit", "manage"])


class KnowledgeBaseViewSet(viewsets.ModelViewSet):
    serializer_class = KnowledgeBaseSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        level = "manage" if self.action in {"partial_update", "destroy", "acl"} else "read"
        return knowledge_bases(RequestContext.from_request(self.request), level)

    def perform_create(self, serializer):
        ctx = RequestContext.from_request(self.request)
        require_capability(ctx, "knowledge.create")
        obj = serializer.save(tenant_id=ctx.tenant_id, owner=ctx.user)
        audit(ctx, "knowledge.create", obj)

    def perform_update(self, serializer):
        with transaction.atomic():
            serializer.save()
            bump_epoch(RequestContext.from_request(self.request))

    def perform_destroy(self, obj):
        ctx = RequestContext.from_request(self.request)
        with transaction.atomic():
            obj.deleted_at = timezone.now()
            obj.save(update_fields=["deleted_at"])
            bump_epoch(ctx)
            audit(ctx, "knowledge.delete", obj)

    @action(detail=True, methods=["get", "post"])
    def acl(self, request, pk=None):
        ctx = RequestContext.from_request(request)
        obj = get_knowledge_base(ctx, pk, "manage")
        if request.method == "POST":
            serializer = ACLEntrySerializer(data=request.data, many=True)
            serializer.is_valid(raise_exception=True)
            replace_acl(ctx, obj, serializer.validated_data)
        return Response(ACLEntrySerializer(obj.acl.all(), many=True).data)
