from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PassiveConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.passive"
    label = "passive"
    verbose_name = _("Passive services")
