import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.monitoring.probes import configuration_status, probe_model
from apps.users.models import User
from apps.users.permissions import require_capability
from common.context import RequestContext


class Command(BaseCommand):
    help = "Check model configuration/schema; --live explicitly calls configured endpoints using quota"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true")
        parser.add_argument("--email")
        parser.add_argument("--report")

    def handle(self, *args, **options):
        report = configuration_status()
        if options["live"]:
            if not options["email"]:
                raise CommandError("--live requires --email to select an authorized tenant user")
            user = (
                User.objects.select_related("tenant")
                .filter(email=options["email"], is_active=True, tenant__is_active=True)
                .first()
            )
            if not user:
                raise CommandError("Active user not found")
            ctx = RequestContext.for_user(user)
            require_capability(ctx, "monitoring.read")
            report["probes"] = [probe_model(ctx, kind) for kind in ["embedding", "generation"]]
        report["status"] = (
            "passed"
            if (
                report["schema_matches"]
                and all(m["configured"] for m in report["models"])
                and all(p["status"] == "passed" for p in report.get("probes", []))
            )
            else "failed"
        )
        if options["report"]:
            output = Path(options["report"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps(report, ensure_ascii=True))
        if report["status"] != "passed":
            raise CommandError(
                "Model configuration or protocol probe failed; see safe error codes in the report"
            )
