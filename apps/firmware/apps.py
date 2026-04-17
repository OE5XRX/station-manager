from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class FirmwareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.firmware"
    verbose_name = _("Firmware")
