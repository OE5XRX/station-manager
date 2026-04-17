from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MonitoringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.monitoring"
    verbose_name = _("Monitoring")
