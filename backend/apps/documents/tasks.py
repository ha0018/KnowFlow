from celery import shared_task
from django.db.models import Q
from django.utils import timezone


@shared_task
def process_document(task_id):
    from .services import process

    process(task_id)


@shared_task
def recover_work():
    from datetime import timedelta

    from apps.conversations.services import interrupt_stale_runs

    from .models import DocumentTask
    from .services import dispatch

    tasks = DocumentTask.objects.filter(status__in=["PENDING", "RUNNING", "RETRY_WAIT"]).filter(
        Q(lease_until__isnull=True) | Q(lease_until__lt=timezone.now())
    )
    for task_id in tasks.values_list("id", flat=True)[:100]:
        dispatch(task_id)
    interrupt_stale_runs()
    from apps.monitoring.metering import recover_stale_reservations

    recover_stale_reservations()
    from apps.evaluation.models import EvalRun
    from apps.evaluation.services import dispatch_evaluation

    for run_id in EvalRun.objects.filter(status="PENDING").values_list("id", flat=True)[:20]:
        dispatch_evaluation(run_id)
    EvalRun.objects.filter(status="RUNNING").filter(
        Q(heartbeat_at__lt=timezone.now() - timedelta(minutes=10))
        | Q(heartbeat_at__isnull=True, started_at__lt=timezone.now() - timedelta(minutes=10))
    ).update(status="FAILED", metrics={"error": "WORKER_INTERRUPTED"}, finished_at=timezone.now())


@shared_task
def cleanup_data(after="", storage_only=False):
    from django.conf import settings
    from django.core.management import call_command

    from .maintenance import cleanup_storage

    if not storage_only:
        call_command("cleanup_expired", apply=True, parser_files=True)
    if settings.CLEANUP_STORAGE_ENABLED:
        report = cleanup_storage(apply=True, grace_days=settings.CLEANUP_STORAGE_GRACE_DAYS, after=after)
        if report["next_cursor"]:
            cleanup_data.apply_async(
                kwargs={"after": report["next_cursor"], "storage_only": True}, countdown=5
            )
        return report
    return {"storage": "disabled"}
