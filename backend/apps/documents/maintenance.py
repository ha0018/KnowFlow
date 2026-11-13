import re
import shutil
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.users.models import Tenant
from common.models import AuditEvent

from .models import DocumentVersion, StorageObject


def cleanup_parser_files(*, apply=False, root=None):
    root = Path(root or tempfile.gettempdir()).resolve()
    cutoff = timezone.now().timestamp() - 24 * 3600
    report = {"eligible": 0, "removed": 0, "busy": 0}
    for path in root.glob("knowflow-parse-*"):
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != root:
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        report["eligible"] += 1
        if apply:
            try:
                # Resolve and verify the absolute target immediately before rmtree.
                target = path.resolve()
                if target.parent != root or not target.name.startswith("knowflow-parse-"):
                    continue
                shutil.rmtree(target)
                report["removed"] += 1
            except PermissionError:
                report["busy"] += 1
    return report


def safe_key(key, tenant_id):
    return bool(
        re.fullmatch(
            rf"tenants/{re.escape(str(tenant_id))}/[a-f0-9]{{32}}(?:_[A-Za-z0-9]{{7}})?\.(?:pdf|docx|txt|md)",
            key,
        )
    )


def safe_local_key(key):
    if settings.STORAGES["default"]["BACKEND"] != "django.core.files.storage.FileSystemStorage":
        return True
    root = Path(settings.MEDIA_ROOT).resolve()
    path = root / key
    return path.resolve().is_relative_to(root) and not any(
        p.is_symlink() for p in [path, *path.parents] if p != root.parent
    )


def cleanup_storage(*, apply=False, grace_days=7, limit=1000, after="", tenant_id=None):
    if not 1 <= grace_days <= 365 or not 1 <= limit <= 10000:
        raise ValueError("Invalid cleanup retention or batch size")
    cutoff = timezone.now() - timedelta(days=grace_days)
    tenants = Tenant.objects.order_by("id")
    if tenant_id:
        tenants = tenants.filter(pk=tenant_id)
    report = {
        "mode": "apply" if apply else "dry_run",
        "grace_days": grace_days,
        "objects": [],
        "next_cursor": None,
    }
    count = 0
    deadline = time.monotonic() + 120
    for tenant in tenants:
        prefix = f"tenants/{tenant.pk}"
        try:
            _, files = default_storage.listdir(prefix)
        except FileNotFoundError:
            continue
        for filename in sorted(files):
            key = prefix + "/" + filename
            if key <= after or not safe_key(key, tenant.pk) or not safe_local_key(key):
                continue
            if count >= limit or (count and time.monotonic() >= deadline):
                report["next_cursor"] = report["objects"][-1]["key"]
                return report
            count += 1
            item = {"key": key, "status": "referenced"}
            report["objects"].append(item)
            # Keep the lock through the delete, serialized with upload finalization.
            with transaction.atomic():
                Tenant.objects.select_for_update().get(pk=tenant.pk)
                if DocumentVersion.objects.filter(file=key).exists():
                    if apply:
                        StorageObject.objects.filter(key=key).update(
                            state="available", first_unreferenced_at=None
                        )
                    continue
                modified = default_storage.get_modified_time(key)
                size = default_storage.size(key)
                row = StorageObject.objects.filter(key=key).first()
                if row and row.state == "writing" and row.lease_until and row.lease_until > timezone.now():
                    item["status"] = "upload_in_progress"
                    continue
                changed = row is None or row.observed_modified_at != modified or row.observed_size != size
                first_seen = None if changed else row.first_unreferenced_at
                eligible = modified < cutoff and first_seen is not None and first_seen < cutoff
                item["status"] = "eligible" if eligible else "quarantine"
                if not apply:
                    continue
                if row is None:
                    row = StorageObject(tenant=tenant, key=key)
                row.state, row.lease_until = "candidate", None
                row.observed_modified_at, row.observed_size = modified, size
                row.first_unreferenced_at = first_seen or timezone.now()
                row.save()
                if eligible:
                    # All immutable versions, including deleted documents, remain protected.
                    default_storage.delete(key)
                    row.state = "deleted"
                    row.save(update_fields=["state", "updated_at"])
                    AuditEvent.objects.create(
                        tenant=tenant,
                        action="storage.orphan.delete",
                        resource_type="documents.storageobject",
                        resource_id=str(row.pk),
                        trace_id=f"cleanup:{row.pk.hex}",
                    )
                    item["status"] = "deleted"
    return report
