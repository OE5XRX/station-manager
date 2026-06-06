from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class StationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.stations"
    verbose_name = _("Stations")

    def ready(self):
        # Register signal handlers for topology audit-log emission.
        # Import inside ready() per Django convention to avoid
        # AppRegistry-not-ready issues at startup.
        from apps.stations import signals  # noqa: F401
