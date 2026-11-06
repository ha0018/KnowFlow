from django.core.management.base import BaseCommand

from apps.documents.tasks import recover_work


class Command(BaseCommand):
    help = "Redeliver pending/expired document jobs and interrupt stale answer runs"

    def handle(self, *args, **options):
        recover_work()
        self.stdout.write("Recovery scan complete")
