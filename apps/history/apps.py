from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class HistoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.history"
    label = "history"
    verbose_name = _("Telemetry history")
