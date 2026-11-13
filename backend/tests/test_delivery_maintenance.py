import io
import os
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.utils import timezone

from ai_engine.parsers import ParseError, parse
from apps.documents.maintenance import cleanup_storage
from apps.documents.models import Document, StorageObject

pytestmark = pytest.mark.django_db(transaction=True)


def orphan(world, settings, *, age_days=10):
    key = f"tenants/{world['tenant'].pk}/{uuid.uuid4().hex}.md"
    default_storage.save(key, ContentFile(b"unreferenced"))
    timestamp = (timezone.now() - timedelta(days=age_days)).timestamp()
    os.utime(Path(settings.MEDIA_ROOT) / key, (timestamp, timestamp))
    return key


def test_cleanup_requires_two_pass_retention_and_protects_all_versions(world, ingest, settings):
    doc, version, *_ = ingest()
    Document.objects.filter(pk=doc.pk).update(deleted_at=timezone.now())
    key = orphan(world, settings)
    assert cleanup_storage()["mode"] == "dry_run" and not StorageObject.objects.filter(key=key).exists()
    first = cleanup_storage(apply=True)
    assert next(v for v in first["objects"] if v["key"] == key)["status"] == "quarantine"
    assert default_storage.exists(key) and default_storage.exists(version.file.name)
    StorageObject.objects.filter(key=key).update(first_unreferenced_at=timezone.now() - timedelta(days=8))
    assert next(v for v in cleanup_storage()["objects"] if v["key"] == key)["status"] == "eligible"
    assert default_storage.exists(key)
    cleanup_storage(apply=True)
    assert not default_storage.exists(key) and default_storage.exists(version.file.name)
    assert doc.versions.count() == 1


def test_cleanup_ignores_upload_lease_and_resets_changed_object(world, settings):
    key = orphan(world, settings)
    row = StorageObject.objects.create(
        tenant=world["tenant"], key=key, lease_until=timezone.now() + timedelta(minutes=5)
    )
    assert cleanup_storage(apply=True)["objects"][0]["status"] == "upload_in_progress"
    row.lease_until = timezone.now() - timedelta(seconds=1)
    row.save()
    cleanup_storage(apply=True)
    StorageObject.objects.filter(pk=row.pk).update(first_unreferenced_at=timezone.now() - timedelta(days=8))
    default_storage.open(key, "wb").write(b"changed")
    cleanup_storage(apply=True)
    row.refresh_from_db()
    assert row.first_unreferenced_at > timezone.now() - timedelta(minutes=1) and default_storage.exists(key)


def test_cleanup_pagination_and_foreign_namespace(world, settings):
    keys = sorted(orphan(world, settings) for _ in range(3))
    default_storage.save("unmanaged/keep.md", ContentFile(b"keep"))
    first = cleanup_storage(limit=2)
    assert first["next_cursor"] == keys[1]
    second = cleanup_storage(limit=2, after=first["next_cursor"])
    assert [v["key"] for v in second["objects"]] == [keys[2]]
    assert default_storage.exists("unmanaged/keep.md")


def test_expired_cleanup_keeps_live_tokens(world):
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
    from rest_framework_simplejwt.tokens import RefreshToken

    live = RefreshToken.for_user(world["member"])
    old = OutstandingToken.objects.create(
        user=world["member"],
        jti=uuid.uuid4().hex,
        token="expired-fixture",
        expires_at=timezone.now() - timedelta(days=1),
    )
    call_command("cleanup_expired", stdout=io.StringIO())
    assert OutstandingToken.objects.filter(pk=old.pk).exists()
    call_command("cleanup_expired", apply=True, stdout=io.StringIO())
    assert not OutstandingToken.objects.filter(pk=old.pk).exists()
    assert OutstandingToken.objects.filter(jti=live["jti"]).exists()


def test_parser_timeout_kills_actual_process_and_has_no_late_write(settings, monkeypatch, tmp_path):
    settings.PARSER_TIMEOUT_SECONDS = 0.15
    original = subprocess.Popen
    marker = tmp_path / "late"
    processes = []

    def slow(args, **kwargs):
        process = original(
            [
                sys.executable,
                "-c",
                "import time,pathlib; time.sleep(2); pathlib.Path("
                + repr(str(marker))
                + ").write_text('late')",
            ],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr("ai_engine.parsers.isolation.subprocess.Popen", slow)
    with pytest.raises(ParseError, match="PARSER_TIMEOUT"):
        parse("a.md", b"text")
    assert processes[0].poll() is not None and not marker.exists()


def test_parser_revoke_check_stops_pending_process(monkeypatch):
    original = subprocess.Popen
    processes = []

    def slow(args, **kwargs):
        process = original([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        processes.append(process)
        return process

    def revoked():
        raise ValueError("DOCUMENT_UNAVAILABLE")

    monkeypatch.setattr("ai_engine.parsers.isolation.subprocess.Popen", slow)
    with pytest.raises(ValueError, match="DOCUMENT_UNAVAILABLE"):
        parse("a.md", b"text", check=revoked)
    assert processes[0].poll() is not None


def test_parser_environment_excludes_model_and_storage_secrets(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "must-not-enter-parser")
    monkeypatch.setenv("DATABASE_URL", "must-not-enter-parser")
    original = subprocess.Popen
    environments = []

    def capture(args, **kwargs):
        environments.append(kwargs["env"])
        return original(args, **kwargs)

    monkeypatch.setattr("ai_engine.parsers.isolation.subprocess.Popen", capture)
    assert parse("a.md", "无凭据解析".encode())[0]["text"] == "无凭据解析"
    assert "AI_API_KEY" not in environments[0] and "DATABASE_URL" not in environments[0]


def test_parser_output_and_text_limits_are_enforced(settings):
    settings.PARSER_OUTPUT_BYTES = 1024
    with pytest.raises(ParseError, match="PARSER_OUTPUT_LIMIT"):
        parse("a.md", ("内容" * 1000).encode())
    settings.MAX_PARSED_CHARS = 10
    with pytest.raises(ParseError, match="TEXT_TOO_LARGE"):
        parse("a.md", b"a" * 20)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Deployment CPU/address-space limits use Linux rlimit"
)
@pytest.mark.parametrize("resource_name", ["memory", "cpu"])
def test_native_linux_parser_resource_limits_kill_or_reject_allocation(resource_name):
    from ai_engine.parsers import worker

    script = (
        "import importlib.util; "
        f"s=importlib.util.spec_from_file_location('parser_worker',{str(Path(worker.__file__).resolve())!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "m.apply_limits({'memory_mb':128,'cpu_seconds':1,'output_bytes':1024*1024});\n"
    )
    if resource_name == "memory":
        script += "try:\n bytearray(256*1024*1024)\nexcept MemoryError:\n print('allocation_rejected')\n"
    else:
        script += "while True: pass\n"
    result = subprocess.run([sys.executable, "-I", "-c", script], timeout=5, capture_output=True)
    if resource_name == "memory":
        assert result.returncode == 0 and b"allocation_rejected" in result.stdout
    else:
        assert result.returncode < 0
