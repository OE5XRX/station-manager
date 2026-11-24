from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ModuleFirmwareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.module_firmware"
    verbose_name = _("Module Firmware")

    def ready(self):
        pass
