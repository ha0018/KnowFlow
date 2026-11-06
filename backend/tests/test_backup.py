import hashlib
import sqlite3

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


def test_backup_restore_preserves_database_and_file_hashes(ingest, tmp_path):
    if connection.vendor != "sqlite":
        pytest.skip("Local snapshot command is specific to SQLite")
    doc, version, _, _ = ingest()
    backup = tmp_path / "backup.zip"
    destination = tmp_path / "restored"
    call_command("backup_local", output=str(backup))
    call_command("restore_local", archive=str(backup), destination=str(destination))
    with sqlite3.connect(destination / "db.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM documents_document").fetchone()[0] == 1
    assert (
        hashlib.sha256((destination / "media" / version.file.name).read_bytes()).hexdigest() == version.sha256
    )
