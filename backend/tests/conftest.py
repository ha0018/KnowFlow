import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.documents.services import upload
from apps.knowledge.models import KnowledgeBase
from apps.users.models import Tenant, User
from common.context import RequestContext


@pytest.fixture(autouse=True)
def local_storage(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def world():
    tenant = Tenant.objects.create(name="甲企业", slug="alpha")
    other = Tenant.objects.create(name="乙企业", slug="beta")
    admin = User.objects.create_user(
        "admin@alpha.test", "test-password-2026", tenant=tenant, is_tenant_admin=True
    )
    member = User.objects.create_user("member@alpha.test", "test-password-2026", tenant=tenant)
    outsider = User.objects.create_user(
        "admin@beta.test", "test-password-2026", tenant=other, is_tenant_admin=True
    )
    kb = KnowledgeBase.objects.create(tenant=tenant, owner=admin, name="团队资料", visibility="tenant")
    private = KnowledgeBase.objects.create(tenant=tenant, owner=admin, name="秘密资料")
    return {
        "tenant": tenant,
        "other": other,
        "admin": admin,
        "member": member,
        "outsider": outsider,
        "kb": kb,
        "private": private,
        "ctx": RequestContext.for_user(admin),
    }


@pytest.fixture
def client_for():
    def make(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    return make


@pytest.fixture
def ingest(world):
    def make(
        text="退款在购买后30天内可以申请。超过30天不支持退款。", name="退款政策.md", kb=None, document_id=None
    ):
        return upload(
            world["ctx"],
            (kb or world["kb"]).pk,
            SimpleUploadedFile(name, text.encode(), content_type="text/markdown"),
            document_id=document_id,
        )

    return make
