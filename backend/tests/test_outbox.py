from unittest.mock import patch

import pytest
from kombu.exceptions import OperationalError

from apps.documents.models import DocumentTask
from apps.documents.tasks import recover_work

pytestmark = pytest.mark.django_db(transaction=True)


def test_failed_publication_keeps_recoverable_database_task(ingest):
    with patch("apps.documents.tasks.process_document.delay", side_effect=OperationalError("queue down")):
        doc, _, build, task = ingest()
    task.refresh_from_db()
    assert task.status == "PENDING" and task.attempt == 0 and task.celery_task_id == ""
    assert DocumentTask.objects.filter(pk=task.pk).exists()
    recover_work()
    task.refresh_from_db()
    doc.refresh_from_db()
    assert task.status == "READY" and task.attempt == 1
    assert doc.active_build_id == build.pk
