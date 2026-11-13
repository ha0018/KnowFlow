import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.documents.archives import export_objects, restore_objects, verify_objects


class Command(BaseCommand):
    help = "Export/import/verify private object snapshots against a quiesced database; deployment helper"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["export", "import", "verify"])
        parser.add_argument("--archive", required=True)
        parser.add_argument("--quiesced", action="store_true", required=True)
        parser.add_argument("--report")

    def handle(self, **options):
        try:
            result = {"export": export_objects, "import": restore_objects, "verify": verify_objects}[
                options["action"]
            ](options["archive"])
        except (ValueError, OSError) as exc:
            raise CommandError(type(exc).__name__ + ": snapshot verification failed") from None
        value = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if options["report"]:
            Path(options["report"]).write_text(value + "\n", encoding="utf-8")
        self.stdout.write(value)
