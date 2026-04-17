from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class TunnelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tunnel"
    verbose_name = _("Terminal")
