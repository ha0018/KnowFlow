from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.agents.api import AgentCallsView, AgentReferenceView, ToolsView
from apps.agents.stream import agent_stream
from apps.conversations.api import CancelRunView, ConversationViewSet, FeedbackView, ReferenceView, RunView
from apps.conversations.stream import messages_stream
from apps.documents.api import DocumentViewSet, TaskView, UploadView
from apps.evaluation.api import DatasetsView, EvalResultsView, EvalReviewView, EvalRunsView
from apps.knowledge.api import KnowledgeBaseViewSet
from apps.knowledge.retrieval_api import SearchView
from apps.monitoring.api import CallsView, DashboardView, live, ready
from apps.monitoring.model_api import ModelsView, QuotaView
from apps.users.api import LoginView, LogoutView, MemberRolesView, MembersView, MeView, RefreshView, RolesView

router = DefaultRouter(trailing_slash=False)
router.register("knowledge-bases", KnowledgeBaseViewSet, basename="knowledge-base")
router.register("documents", DocumentViewSet, basename="document")
router.register("conversations", ConversationViewSet, basename="conversation")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/live", live),
    path("health/ready", ready),
    path("api/v1/auth/login", LoginView.as_view()),
    path("api/v1/auth/refresh", RefreshView.as_view()),
    path("api/v1/auth/logout", LogoutView.as_view()),
    path("api/v1/auth/me", MeView.as_view()),
    path("api/v1/members", MembersView.as_view()),
    path("api/v1/roles", RolesView.as_view()),
    path("api/v1/members/<uuid:member_id>/roles", MemberRolesView.as_view()),
    path("api/v1/knowledge-bases/<uuid:kb_id>/documents", UploadView.as_view()),
    path("api/v1/tasks/<uuid:task_id>", TaskView.as_view()),
    path("api/v1/conversations/<uuid:conversation_id>/messages:stream", messages_stream),
    path("api/v1/runs/<uuid:run_id>", RunView.as_view()),
    path("api/v1/runs/<uuid:run_id>/cancel", CancelRunView.as_view()),
    path("api/v1/references/<uuid:reference_id>", ReferenceView.as_view()),
    path("api/v1/messages/<uuid:message_id>/feedback", FeedbackView.as_view()),
    path("api/v1/agents/tools", ToolsView.as_view()),
    path("api/v1/agents/calls", AgentCallsView.as_view()),
    path("api/v1/agents/references/<uuid:call_id>", AgentReferenceView.as_view()),
    path("api/v1/conversations/<uuid:conversation_id>/agent:stream", agent_stream),
    path("api/v1/retrieval/search", SearchView.as_view()),
    path("api/v1/evaluations/datasets", DatasetsView.as_view()),
    path("api/v1/evaluations/runs", EvalRunsView.as_view()),
    path("api/v1/evaluations/runs/<uuid:run_id>", EvalResultsView.as_view()),
    path("api/v1/evaluations/runs/<uuid:run_id>/results/<uuid:result_id>/review", EvalReviewView.as_view()),
    path("api/v1/monitoring/dashboard", DashboardView.as_view()),
    path("api/v1/monitoring/calls", CallsView.as_view()),
    path("api/v1/monitoring/models", ModelsView.as_view()),
    path("api/v1/monitoring/quota", QuotaView.as_view()),
    path("api/v1/", include(router.urls)),
]
