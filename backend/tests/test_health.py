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
