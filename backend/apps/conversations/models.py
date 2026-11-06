from django.conf import settings
from django.db import models

from common.models import TenantEntity


class Conversation(TenantEntity):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField(max_length=160, default="新对话")


class ConversationKnowledgeBase(TenantEntity):
    conversation = models.ForeignKey(Conversation, related_name="scope", on_delete=models.CASCADE)
    knowledge_base = models.ForeignKey("knowledge.KnowledgeBase", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["conversation", "knowledge_base"], name="conversation_kb_unique")
        ]


class Message(TenantEntity):
    conversation = models.ForeignKey(Conversation, related_name="messages", on_delete=models.CASCADE)
    seq = models.PositiveIntegerField()
    role = models.CharField(max_length=12, choices=[("user", "用户"), ("assistant", "助手")])
    content = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="completed")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["seq"]
        constraints = [models.UniqueConstraint(fields=["conversation", "seq"], name="unique_message_seq")]


class Run(TenantEntity):
    conversation = models.ForeignKey(Conversation, related_name="runs", on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    input_message = models.ForeignKey(Message, on_delete=models.PROTECT, related_name="input_runs")
    output_message = models.OneToOneField(Message, on_delete=models.PROTECT, related_name="output_run")
    kind = models.CharField(max_length=10, default="rag")
    status = models.CharField(max_length=20, default="streaming")
    idempotency_key = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    config_snapshot = models.JSONField(default=dict)
    acl_epoch = models.PositiveBigIntegerField()
    trace_id = models.CharField(max_length=64)
    heartbeat_at = models.DateTimeField(auto_now_add=True)
    usage = models.JSONField(default=dict)
    error_code = models.CharField(max_length=80, blank=True)

    class Meta(TenantEntity.Meta):
        constraints = [
            models.UniqueConstraint(fields=["user", "idempotency_key"], name="unique_user_run_key")
        ]


class MessageReference(TenantEntity):
    message = models.ForeignKey(Message, related_name="references", on_delete=models.CASCADE)
    chunk = models.ForeignKey("documents.DocumentChunk", on_delete=models.PROTECT)
    source_id = models.CharField(max_length=12)
    rank = models.PositiveIntegerField()
    locator_snapshot = models.JSONField(default=dict)
    excerpt_snapshot = models.TextField()
    retrieval_scores = models.JSONField(default=dict)

    class Meta(TenantEntity.Meta):
        ordering = ["rank", "id"]


class MessageFeedback(TenantEntity):
    message = models.ForeignKey(Message, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    rating = models.SmallIntegerField(choices=[(-1, "无帮助"), (1, "有帮助")])
    reason = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["message", "user"], name="unique_message_feedback")]
