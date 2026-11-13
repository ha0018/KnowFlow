import json
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.documents.maintenance import cleanup_storage


class Command(BaseCommand):
    help = "Inventory or remove delayed unreferenced upload objects; dry-run by default"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--grace-days", type=int, default=7)
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--after", default="")
        parser.add_argument("--tenant-id")
        parser.add_argument("--output")

    def handle(self, **options):
        report = cleanup_storage(
            **{key: options[key] for key in ("apply", "grace_days", "limit", "after", "tenant_id")}
        )
        value = json.dumps(report, ensure_ascii=False, indent=2)
        if options["output"]:
            Path(options["output"]).write_text(value + "\n", encoding="utf-8")
        else:
            self.stdout.write(value)
