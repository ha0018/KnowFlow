from unittest.mock import Mock, patch

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db(transaction=True)


def test_health_is_public_and_reports_database_readiness():
    client = Client()
    assert client.get("/health/live").json()["status"] == "alive"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["dependencies"]["database"] == "ok"
    assert ready.json()["model"] == "demo"


def test_broker_outage_marks_unready_without_failing_liveness(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    with patch("redis.Redis.from_url") as create:
        create.return_value.ping.side_effect = ConnectionError("queue unavailable")
        response = Client().get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"]["redis"] == "unavailable"
    assert Client().get("/health/live").status_code == 200


def test_storage_outage_marks_unready(settings):
    settings.S3_ENABLED = True
    storage = Mock()
    storage.connection.meta.client.head_bucket.side_effect = ConnectionError("object store unavailable")
    with patch("django.core.files.storage.default_storage", storage):
        response = Client().get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"]["object_storage"] == "unavailable"
