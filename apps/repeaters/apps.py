from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RepeatersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.repeaters"
    label = "repeaters"
    verbose_name = _("Repeaters")
