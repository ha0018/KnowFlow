import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from apps.agents.models import BusinessIssue, ToolCall
from apps.agents.services import execute
from apps.evaluation.models import EvalRun
from apps.evaluation.services import perform_evaluation
from apps.users.models import User

pytestmark = pytest.mark.django_db(transaction=True)


def test_jwt_refresh_rotation_logout_and_disabled_user(world):
    client = APIClient()
    login = client.post(
        "/api/v1/auth/login", {"email": world["admin"].email, "password": "test-password-2026"}, format="json"
    )
    assert login.status_code == 200
    creds = login.json()
    fresh = client.post("/api/v1/auth/refresh", {"refresh": creds["refresh"]}, format="json")
    assert fresh.status_code == 200
    assert (
        client.post("/api/v1/auth/refresh", {"refresh": creds["refresh"]}, format="json").status_code == 401
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {fresh.json()['access']}")
    assert client.get("/api/v1/auth/me").status_code == 200
    assert (
        client.post("/api/v1/auth/logout", {"refresh": fresh.json()["refresh"]}, format="json").status_code
        == 204
    )
    assert (
        client.post("/api/v1/auth/refresh", {"refresh": fresh.json()["refresh"]}, format="json").status_code
        == 401
    )
    User.objects.filter(pk=world["admin"].pk).update(is_active=False)
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize(
    "tool,args",
    [
        ("ShellTool", {"command": "anything"}),
        ("KnowledgeSearchTool", {"query": "退款", "tenant_id": "forged"}),
        ("KnowledgeSearchTool", {"query": "退款", "top_k": 1000}),
        ("DataAnalysisTool", {"date_from": "2026-01-01", "date_to": "2026-12-01", "group_by": "sql"}),
        ("DataAnalysisTool", {"date_from": "2020-01-01", "date_to": "2026-12-01", "group_by": "month"}),
    ],
)
def test_tool_schema_rejects_unsafe_inputs(world, tool, args):
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        execute(world["ctx"], tool, args)


def test_summary_cannot_read_other_tenant(world, ingest):
    from django.http import Http404

    from common.context import RequestContext

    doc, _, _, _ = ingest()
    with pytest.raises(Http404):
        execute(
            RequestContext.for_user(world["outsider"]), "DocumentSummaryTool", {"document_id": str(doc.pk)}
        )
    assert ToolCall.objects.get(tenant=world["other"]).status == "failed"


def test_seed_and_reproducible_evaluation():
    call_command("seed_demo", password="test-password-2026", verbosity=0)
    admin = User.objects.get(email="admin@acme.demo")
    from apps.evaluation.models import EvalDataset

    dataset = EvalDataset.objects.get(tenant=admin.tenant)
    assert dataset.cases.count() == 20
    run = EvalRun.objects.create(
        tenant=admin.tenant,
        dataset=dataset,
        actor=admin,
        config_snapshot={"mode": "hybrid"},
        trace_id="test-eval",
    )
    perform_evaluation(run.pk)
    run.refresh_from_db()
    assert run.status == "COMPLETED" and run.metrics["answerable_evaluated"] == 20
    assert run.results.count() == 20
    assert 0 <= run.metrics["doc_recall_at_5"] <= 1
    count = BusinessIssue.objects.count()
    call_command("seed_demo", password="different-password", verbosity=0)
    assert BusinessIssue.objects.count() == count
    assert User.objects.get(pk=admin.pk).check_password("test-password-2026")
