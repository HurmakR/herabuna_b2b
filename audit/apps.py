from django.apps import AppConfig

class AuditConfig(AppConfig):
    name = "audit"
    verbose_name = "Audit"

    def ready(self) -> None:
        # Import signal handlers at app startup.
        from . import signals  # noqa: F401
