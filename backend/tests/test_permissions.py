import pytest
from django.db import IntegrityError, transaction

from ai_engine.retrieval import search
from apps.documents.models import Document, DocumentACL, DocumentChunk
from apps.documents.selectors import documents
from apps.knowledge.models import KnowledgeBaseACL
from apps.knowledge.services import replace_acl
from apps.users.models import Role, UserRole
from common.context import RequestContext

pytestmark = pytest.mark.django_db(transaction=True)


def test_tenant_scope_covers_document_download_task_search(world, ingest, client_for):
    doc, _, _, task = ingest()
    client = client_for(world["outsider"])
    for path in [f"/documents/{doc.pk}", f"/documents/{doc.pk}/download", f"/tasks/{task.pk}"]:
        assert client.get("/api/v1" + path).status_code == 404
    assert search(RequestContext.for_user(world["outsider"]), "退款") == []
    assert len(search(world["ctx"], "退款")) > 0


def test_private_kb_and_restricted_document_require_both_grants(world, ingest):
    doc, _, _, _ = ingest(kb=world["private"])
    ctx = RequestContext.for_user(world["member"])
    assert not documents(ctx).filter(pk=doc.pk).exists()
    replace_acl(world["ctx"], world["private"], [{"user_id": world["member"].pk, "access_level": "read"}])
    assert documents(ctx).filter(pk=doc.pk).exists()
    doc.acl_mode = "restricted"
    doc.save()
    assert not documents(ctx).filter(pk=doc.pk).exists()
    DocumentACL.objects.create(
        tenant=world["tenant"], document=doc, user=world["member"], access_level="read"
    )
    assert documents(ctx).filter(pk=doc.pk).exists()
    replace_acl(world["ctx"], world["private"], [])
    assert not documents(ctx).filter(pk=doc.pk).exists()


def test_role_acl_and_cross_tenant_role_link(world, ingest):
    doc, _, _, _ = ingest(kb=world["private"])
    role = Role.objects.create(tenant=world["tenant"], code="reader", name="读取")
    UserRole.objects.create(tenant=world["tenant"], user=world["member"], role=role)
    KnowledgeBaseACL.objects.create(
        tenant=world["tenant"], knowledge_base=world["private"], role=role, access_level="read"
    )
    assert documents(RequestContext.for_user(world["member"])).filter(pk=doc.pk).exists()
    with pytest.raises(IntegrityError), transaction.atomic():
        UserRole.objects.create(tenant=world["other"], user=world["outsider"], role=role)


def test_db_rejects_cross_tenant_document_and_wrong_active_build(world, ingest):
    doc, _, build, _ = ingest()
    with pytest.raises(IntegrityError), transaction.atomic():
        Document.objects.create(tenant=world["other"], knowledge_base=world["kb"], title="越权")
    second = Document.objects.create(tenant=world["tenant"], knowledge_base=world["kb"], title="其他文档")
    with pytest.raises(IntegrityError), transaction.atomic():
        Document.objects.filter(pk=second.pk).update(active_build=build)
    with pytest.raises(IntegrityError), transaction.atomic():
        DocumentChunk.objects.filter(document=doc).update(document=second)


def test_acl_subject_xor_is_enforced(world):
    with pytest.raises(IntegrityError), transaction.atomic():
        KnowledgeBaseACL.objects.create(
            tenant=world["tenant"], knowledge_base=world["kb"], access_level="read"
        )


def test_members_cannot_upload_or_manage_other_users(world, client_for):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client = client_for(world["member"])
    assert (
        client.post(
            f"/api/v1/knowledge-bases/{world['kb'].pk}/documents",
            {"file": SimpleUploadedFile("a.txt", b"hello")},
            format="multipart",
        ).status_code
        == 404
    )
    assert client.get("/api/v1/members").status_code == 403


def test_deleted_document_disappears_immediately(world, ingest, client_for):
    doc, _, _, _ = ingest()
    assert client_for(world["admin"]).delete(f"/api/v1/documents/{doc.pk}").status_code == 204
    assert search(world["ctx"], "退款") == []
