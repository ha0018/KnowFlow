"""Deployment object snapshots. Call only while application writers are stopped."""

import hashlib
import json
import uuid
import zipfile

from django.apps import apps
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.serializers.json import DjangoJSONEncoder

from .maintenance import safe_key, safe_local_key
from .models import DocumentVersion

MAX_BACKUP_BYTES = 2 * 1024**3


class SnapshotEncoder(DjangoJSONEncoder):
    def default(self, value):
        if hasattr(value, "tolist"):
            return value.tolist()
        return super().default(value)


def database_inventory():
    result = {}
    labels = {
        "users",
        "knowledge",
        "documents",
        "conversations",
        "agents",
        "evaluation",
        "monitoring",
        "token_blacklist",
        "sessions",
    }
    for model in apps.get_models(include_auto_created=True):
        if model._meta.app_label not in labels or model._meta.proxy:
            continue
        digest, count = hashlib.sha256(), 0
        for row in model._default_manager.order_by(model._meta.pk.name).values().iterator(chunk_size=500):
            digest.update(
                json.dumps(
                    row, cls=SnapshotEncoder, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            digest.update(b"\n")
            count += 1
        result[model._meta.db_table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def stream_hash(source, target=None):
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
        if target:
            target.write(chunk)
    return digest.hexdigest(), size


def expected_files():
    files = {}
    for row in DocumentVersion.objects.values("file", "size_bytes", "sha256", "tenant_id").iterator():
        key = row["file"]
        if not safe_key(key, row["tenant_id"]) or not safe_local_key(key):
            raise ValueError("UNSAFE_SNAPSHOT_KEY")
        item = {"key": key, "size": row["size_bytes"], "sha256": row["sha256"]}
        if key in files and files[key] != item:
            raise ValueError("SNAPSHOT_PROVENANCE_CONFLICT")
        files[key] = item
    if sum(f["size"] for f in files.values()) > MAX_BACKUP_BYTES:
        raise ValueError("SNAPSHOT_TOO_LARGE")
    return files


def export_objects(destination):
    files = expected_files()
    manifest = {
        "schema": "knowflow-objects-v1",
        "inventory": database_inventory(),
        "files": list(files.values()),
    }
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
        for key, item in files.items():
            with default_storage.open(key, "rb") as source, archive.open("objects/" + key, "w") as target:
                digest, size = stream_hash(source, target)
            if digest != item["sha256"] or size != item["size"]:
                raise ValueError("SOURCE_OBJECT_CHECKSUM_MISMATCH")
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return {"status": "passed", "files": len(files), "tables": len(manifest["inventory"])}


def validate_archive(archive):
    names = archive.namelist()
    if len(names) != len(set(names)) or "manifest.json" not in names:
        raise ValueError("INVALID_OBJECT_ARCHIVE")
    if (
        archive.getinfo("manifest.json").file_size > 16 * 1024**2
        or sum(i.file_size for i in archive.infolist()) > MAX_BACKUP_BYTES + 16 * 1024**2
    ):
        raise ValueError("SNAPSHOT_TOO_LARGE")
    manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("schema") != "knowflow-objects-v1" or not isinstance(manifest.get("files"), list):
        raise ValueError("INVALID_OBJECT_ARCHIVE")
    files = {}
    for item in manifest["files"]:
        key = item["key"]
        try:
            tenant_id = uuid.UUID(key.split("/")[1])
        except (ValueError, IndexError) as exc:
            raise ValueError("UNSAFE_SNAPSHOT_KEY") from exc
        if not safe_key(key, tenant_id) or key in files:
            raise ValueError("UNSAFE_SNAPSHOT_KEY")
        member = "objects/" + key
        if member not in names or archive.getinfo(member).file_size != item["size"]:
            raise ValueError("OBJECT_ARCHIVE_MANIFEST_MISMATCH")
        with archive.open(member) as source:
            digest, size = stream_hash(source)
        if digest != item["sha256"] or size != item["size"]:
            raise ValueError("OBJECT_ARCHIVE_CHECKSUM_MISMATCH")
        files[key] = item
    if set(names) != {"manifest.json", *("objects/" + k for k in files)}:
        raise ValueError("UNEXPECTED_OBJECT_ARCHIVE_MEMBER")
    if files != expected_files() or manifest["inventory"] != database_inventory():
        raise ValueError("DATABASE_SNAPSHOT_MISMATCH")
    return manifest, files


def restore_objects(source):
    with zipfile.ZipFile(source) as archive:
        _, files = validate_archive(archive)
        try:
            dirs, names = default_storage.listdir("")
        except FileNotFoundError:
            dirs, names = [], []
        if dirs or names:
            raise ValueError("RESTORE_STORAGE_MUST_BE_EMPTY")
        # Every checksum and DB row digest is verified before the first object write.
        for key in files:
            with archive.open("objects/" + key) as entry:
                written = default_storage.save(key, File(entry))
            if written != key:
                raise ValueError("RESTORE_OBJECT_NAME_COLLISION")
    return verify_objects(source)


def verify_objects(source):
    with zipfile.ZipFile(source) as archive:
        manifest, files = validate_archive(archive)
        for key, item in files.items():
            with default_storage.open(key, "rb") as entry:
                digest, size = stream_hash(entry)
            if digest != item["sha256"] or size != item["size"]:
                raise ValueError("RESTORED_OBJECT_CHECKSUM_MISMATCH")
    return {
        "status": "passed",
        "files": len(files),
        "tables": manifest["inventory"],
        "all_object_sha256_match": True,
        "all_database_row_digests_match": True,
    }
