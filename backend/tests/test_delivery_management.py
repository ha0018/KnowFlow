import pytest
from django.db import IntegrityError, transaction

from apps.documents.models import StorageObject
from apps.knowledge.models import KnowledgeBaseACL
from apps.users.models import Permission, Role, User

pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_member_role_workflow_revokes_existing_capability(world, client_for):
    client = client_for(world["admin"])
    Permission.objects.get_or_create(code="agent.execute")
    created = client.post(
        "/api/v1/members",
        {"email": "new@alpha.test", "display_name": "新成员", "password": "NewMember!2026"},
        format="json",
    )
    assert created.status_code == 201
    member = User.objects.get(pk=created.json()["id"])
    role = client.post(
        "/api/v1/roles",
        {"code": "analyst", "name": "分析师", "permission_codes": ["agent.execute"]},
        format="json",
    )
    assert role.status_code == 201
    rid = role.json()["id"]
    assert (
        client.put(f"/api/v1/members/{member.pk}/roles", {"role_ids": [rid]}, format="json").status_code
        == 204
    )
    saved = next(u for u in client.get("/api/v1/members").json() if u["id"] == str(member.pk))
    assert str(rid) in saved["role_ids"]
    worker = client_for(member)
    assert worker.get("/api/v1/agents/calls").status_code == 200
    before = world["tenant"].acl_epoch
    assert client.patch(f"/api/v1/roles/{rid}", {"permission_codes": []}, format="json").status_code == 200
    assert worker.get("/api/v1/agents/calls").status_code == 403
    world["tenant"].refresh_from_db()
    assert world["tenant"].acl_epoch > before
    assert (
        client.patch(f"/api/v1/members/{member.pk}", {"is_active": False}, format="json").status_code == 200
    )
    member.refresh_from_db()
    assert not member.is_active


@pytest.mark.parametrize("endpoint", ["members", "roles", "permissions", "authorization/subjects"])
def test_nonadmin_cannot_enumerate_management(world, client_for, endpoint):
    assert client_for(world["member"]).get("/api/v1/" + endpoint).status_code == 403


def test_empty_upload_returns_the_parser_error_code_before_storage(world, client_for):
    from django.core.files.uploadedfile import SimpleUploadedFile

    response = client_for(world["admin"]).post(
        f"/api/v1/knowledge-bases/{world['kb'].pk}/documents",
        {"file": SimpleUploadedFile("empty.md", b"")},
        format="multipart",
    )
    assert response.status_code == 400 and "EMPTY_FILE" in response.content.decode()
    assert not StorageObject.objects.exists()


def test_management_rejects_cross_tenant_and_privilege_fields(world, client_for):
    client = client_for(world["admin"])
    foreign = Role.objects.create(tenant=world["other"], code="foreign", name="外部")
    assert (
        client.patch(
            f"/api/v1/members/{world['outsider'].pk}", {"is_active": False}, format="json"
        ).status_code
        == 404
    )
    assert client.patch(f"/api/v1/roles/{foreign.pk}", {"name": "篡改"}, format="json").status_code == 404
    assert (
        client.put(
            f"/api/v1/members/{world['member'].pk}/roles", {"role_ids": [str(foreign.pk)]}, format="json"
        ).status_code
        == 400
    )
    assert (
        client.patch(
            f"/api/v1/members/{world['member'].pk}", {"is_tenant_admin": True}, format="json"
        ).status_code
        == 400
    )
    assert (
        client.patch(f"/api/v1/members/{world['admin'].pk}", {"is_active": False}, format="json").status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/roles",
            {"code": "bad", "name": "bad", "permission_codes": ["tenant.admin"]},
            format="json",
        ).status_code
        == 400
    )


def test_acl_browser_contract_and_atomic_scope_change(world, ingest, client_for):
    client = client_for(world["admin"])
    doc, *_ = ingest()
    base = f"/api/v1/knowledge-bases/{world['kb'].pk}/access"
    subject = client.get(f"/api/v1/authorization/subjects?knowledge_base_id={world['kb'].pk}")
    assert subject.status_code == 200 and all(
        str(u["id"]) != str(world["outsider"].pk) for u in subject.json()["members"]
    )
    assert client_for(world["member"]).get(base).status_code == 404
    result = client.post(
        base,
        {
            "visibility": "private",
            "entries": [{"user_id": str(world["outsider"].pk), "access_level": "read"}],
        },
        format="json",
    )
    assert result.status_code == 400
    world["kb"].refresh_from_db()
    assert world["kb"].visibility == "tenant"
    result = client.post(
        base,
        {
            "visibility": "private",
            "entries": [{"user_id": str(world["member"].pk), "access_level": "manage"}],
        },
        format="json",
    )
    assert result.status_code == 200
    manager = client_for(world["member"])
    assert manager.get(base).status_code == 200
    assert (
        manager.get(f"/api/v1/authorization/subjects?knowledge_base_id={world['kb'].pk}").status_code == 200
    )
    document = f"/api/v1/documents/{doc.pk}/access"
    assert client.get(document).json()["acl_mode"] == "inherit"
    assert client.post(document, {"acl_mode": "restricted", "entries": []}, format="json").status_code == 200
    assert manager.get(f"/api/v1/documents/{doc.pk}").status_code == 404
    assert client.get(document).status_code == 200


def test_stale_manager_context_cannot_replace_acl(world):
    from django.http import Http404
    from rest_framework.exceptions import NotFound

    from apps.knowledge.services import replace_acl
    from common.context import RequestContext

    KnowledgeBaseACL.objects.create(
        tenant=world["tenant"], knowledge_base=world["private"], user=world["member"], access_level="manage"
    )
    ctx = RequestContext.for_user(world["member"])
    replace_acl(world["ctx"], world["private"], [])
    with pytest.raises((Http404, NotFound)):
        replace_acl(ctx, world["private"], [])


def test_storage_inventory_ownership_is_database_guarded(world):
    row = StorageObject.objects.create(
        tenant=world["tenant"], key=f"tenants/{world['tenant'].pk}/{'a' * 32}.md", state="available"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        StorageObject.objects.filter(pk=row.pk).update(tenant=world["other"])
    with pytest.raises(IntegrityError), transaction.atomic():
        StorageObject.objects.filter(pk=row.pk).update(key="other.md")
