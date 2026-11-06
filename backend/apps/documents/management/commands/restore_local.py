import hashlib
import json
import zipfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Verify and restore a local backup into a new, empty destination; never overwrite a running database"
    )

    def add_arguments(self, parser):
        parser.add_argument("--archive", required=True)
        parser.add_argument("--destination", required=True)

    def handle(self, *args, **options):
        destination = Path(options["destination"]).resolve()
        if destination.exists() and any(destination.iterdir()):
            raise CommandError("Destination must be empty")
        with zipfile.ZipFile(options["archive"]) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if "db.sqlite3" not in manifest or sum(i.file_size for i in archive.infolist()) > 1024**3:
                raise CommandError("Invalid or oversized backup")
            for name, digest in manifest.items():
                path = (destination / name).resolve()
                if not path.is_relative_to(destination) or (
                    name != "db.sqlite3" and not name.startswith("media/tenants/")
                ):
                    raise CommandError("Unsafe archive member")
                if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    raise CommandError("Backup checksum mismatch")
            for name in manifest:
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as target:
                    target.write(archive.read(name))
        self.stdout.write(
            self.style.SUCCESS(
                f"Restored to {destination}; configure DATABASE_URL and MEDIA_ROOT to inspect it"
            )
        )
