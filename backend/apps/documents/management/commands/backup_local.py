import hashlib
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.documents.models import DocumentVersion


class Command(BaseCommand):
    help = "Create a consistent SQLite snapshot plus referenced local files (local development only)"

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        if (
            connection.vendor != "sqlite"
            or settings.STORAGES["default"]["BACKEND"] != "django.core.files.storage.FileSystemStorage"
        ):
            raise CommandError(
                "This command requires SQLite and local storage; use pg_dump and S3 versioned backups for deployment"
            )
        output = Path(options["output"]).resolve()
        if output.exists():
            raise CommandError("Output exists; choose a new archive path")
        output.parent.mkdir(parents=True, exist_ok=True)
        connection.ensure_connection()
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "db.sqlite3"
            with closing(sqlite3.connect(snapshot)) as target:
                connection.connection.backup(target)
            with closing(sqlite3.connect(snapshot)) as snapshot_db:
                files = [
                    row[0]
                    for row in snapshot_db.execute(f"SELECT file FROM {DocumentVersion._meta.db_table}")
                ]
            manifest = {}
            with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
                raw = snapshot.read_bytes()
                archive.writestr("db.sqlite3", raw)
                manifest["db.sqlite3"] = hashlib.sha256(raw).hexdigest()
                for name in sorted(set(files)):
                    path = (Path(settings.MEDIA_ROOT) / name).resolve()
                    if not path.is_relative_to(Path(settings.MEDIA_ROOT).resolve()):
                        raise CommandError("Unexpected media path")
                    raw = path.read_bytes()
                    member = "media/" + name.replace("\\", "/")
                    archive.writestr(member, raw)
                    manifest[member] = hashlib.sha256(raw).hexdigest()
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        self.stdout.write(self.style.SUCCESS(str(output)))
