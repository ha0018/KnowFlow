import json
import zipfile

import pytest
from django.core.files.storage import default_storage

from apps.documents.archives import database_inventory, export_objects, restore_objects, verify_objects

pytestmark = pytest.mark.django_db(transaction=True)


def test_object_snapshot_restores_files_and_row_digests_without_overwrite(ingest, settings, tmp_path):
    _, version, *_ = ingest()
    archive = tmp_path / "objects.zip"
    expected = database_inventory()
    export_objects(archive)
    with pytest.raises(ValueError, match="RESTORE_STORAGE_MUST_BE_EMPTY"):
        restore_objects(archive)
    settings.MEDIA_ROOT = tmp_path / "new-storage"
    result = restore_objects(archive)
    assert result["all_object_sha256_match"] and result["all_database_row_digests_match"]
    assert database_inventory() == expected
    assert (
        default_storage.open(version.file.name).read()
        == "退款在购买后30天内可以申请。超过30天不支持退款。".encode()
    )
    assert verify_objects(archive)["files"] == 1


@pytest.mark.parametrize("tampering", ["content", "traversal", "extra", "duplicate"])
def test_object_import_validates_entire_archive_before_writes(ingest, settings, tmp_path, tampering):
    ingest()
    archive = tmp_path / "original.zip"
    export_objects(archive)
    with zipfile.ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist()}
    first = next(name for name in members if name.startswith("objects/"))
    if tampering == "content":
        members[first] = b"tampered"
    if tampering == "traversal":
        manifest = json.loads(members["manifest.json"])
        manifest["files"][0]["key"] = "../outside"
        members["manifest.json"] = json.dumps(manifest).encode()
    if tampering == "extra":
        members["unexpected"] = b"extra"
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(forged, "x") as target:
        for name, content in members.items():
            target.writestr(name, content)
        if tampering == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                target.writestr("manifest.json", members["manifest.json"])
    settings.MEDIA_ROOT = tmp_path / "empty"
    with pytest.raises(ValueError):
        restore_objects(forged)
    assert not settings.MEDIA_ROOT.exists()


def test_object_restore_rejects_database_divergence_before_writes(world, ingest, settings, tmp_path):
    ingest()
    archive = tmp_path / "objects.zip"
    export_objects(archive)
    world["tenant"].name = "different"
    world["tenant"].save()
    settings.MEDIA_ROOT = tmp_path / "empty"
    with pytest.raises(ValueError, match="DATABASE_SNAPSHOT_MISMATCH"):
        restore_objects(archive)
    assert not settings.MEDIA_ROOT.exists()
