import json

from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken


class Command(BaseCommand):
    help = (
        "Expire sessions/JWTs and recover stale work; dry-run by default; retain business/audit/usage history"
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, **options):
        from apps.conversations.services import interrupt_stale_runs
        from apps.monitoring.metering import recover_stale_reservations

        report = {
            "mode": "apply" if options["apply"] else "dry_run",
            "sessions": Session.objects.filter(expire_date__lt=timezone.now()).count(),
            "tokens": OutstandingToken.objects.filter(expires_at__lt=timezone.now()).count(),
        }
        if options["apply"]:
            call_command("clearsessions", verbosity=0)
            call_command("flushexpiredtokens", verbosity=0)
            interrupt_stale_runs()
            recover_stale_reservations()
        self.stdout.write(json.dumps(report))
