from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SstvConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sstv"
    label = "sstv"
    verbose_name = _("SSTV")

    def ready(self):
        from . import signals  # noqa: F401 — deletes stored files with their rows
