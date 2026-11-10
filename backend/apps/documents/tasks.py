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
