from django.apps import AppConfig


class MonitoringConfig(AppConfig):
    name = "apps.monitoring"

    def ready(self):
        from . import checks  # noqa: F401
