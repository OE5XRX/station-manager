from django.apps import AppConfig


class ModuleFirmwareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.module_firmware"
    verbose_name = "Module Firmware"

    def ready(self):
        pass
