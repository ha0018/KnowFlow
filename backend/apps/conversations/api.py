from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.documents.selectors import chunk_readable
from apps.knowledge.selectors import knowledge_bases
from common.context import RequestContext

from .models import Conversation, ConversationKnowledgeBase, Message, MessageFeedback, MessageReference, Run
from .services import cancel_run, history, message_visible, reference_data


class ConversationSerializer(serializers.ModelSerializer):
    kb_ids = serializers.ListField(child=serializers.UUIDField(), write_only=True, required=False)

    class Meta:
        model = Conversation
        fields = ["id", "title", "kb_ids", "created_at"]
        read_only_fields = ["id", "created_at"]


class ConversationViewSet(viewsets.ModelViewSet):
    serializer_class = ConversationSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return Conversation.objects.filter(tenant_id=self.request.user.tenant_id, user=self.request.user)

    def perform_create(self, serializer):
        ctx = RequestContext.from_request(self.request)
        ids = serializer.validated_data.pop("kb_ids", [])
        if knowledge_bases(ctx).filter(pk__in=ids).count() != len(set(ids)):
            raise ValidationError("知识库不可用")
        with transaction.atomic():
            obj = serializer.save(tenant_id=ctx.tenant_id, user=ctx.user)
            for pk in set(ids):
                ConversationKnowledgeBase.objects.create(
                    tenant_id=ctx.tenant_id, conversation=obj, knowledge_base_id=pk
                )

    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        return Response(history(RequestContext.from_request(request), self.get_object()))


class RunView(APIView):
    def get(self, request, run_id):
        ctx = RequestContext.from_request(request)
        run = get_object_or_404(
            Run.objects.select_related("output_message"), pk=run_id, tenant_id=ctx.tenant_id, user=ctx.user
        )
        visible = message_visible(ctx, run.output_message)
        return Response(
            {
                "id": run.pk,
                "status": run.status,
                "trace_id": run.trace_id,
                "content": run.output_message.content if visible else "此回复包含已撤权资料，内容已隐藏。",
                "usage": run.usage,
                "error_code": run.error_code,
                "references": [reference_data(r) for r in run.output_message.references.all()]
                if visible
                else [],
            }
        )


class CancelRunView(APIView):
    def post(self, request, run_id):
        run = cancel_run(RequestContext.from_request(request), run_id)
        return Response({"id": run.pk, "status": run.status})


class ReferenceView(APIView):
    def get(self, request, reference_id):
        ctx = RequestContext.from_request(request)
        ref = get_object_or_404(
            MessageReference.objects.select_related("chunk__document", "chunk__build__version"),
            pk=reference_id,
            tenant_id=ctx.tenant_id,
            message__conversation__user=ctx.user,
        )
        if not chunk_readable(ctx, ref.chunk_id):
            from django.http import Http404

            raise Http404()
        return Response(
            {**reference_data(ref), "excerpt": ref.excerpt_snapshot, "document_id": ref.chunk.document_id}
        )


class FeedbackInput(serializers.Serializer):
    rating = serializers.ChoiceField(choices=[-1, 1])
    reason = serializers.CharField(max_length=500, required=False, default="")


class FeedbackView(APIView):
    def post(self, request, message_id):
        ctx = RequestContext.from_request(request)
        message = get_object_or_404(
            Message, pk=message_id, tenant_id=ctx.tenant_id, conversation__user=ctx.user, role="assistant"
        )
        serializer = FeedbackInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        MessageFeedback.objects.update_or_create(
            tenant_id=ctx.tenant_id, message=message, user=ctx.user, defaults=serializer.validated_data
        )
        return Response(status=204)
