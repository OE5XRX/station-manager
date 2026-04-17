from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BuilderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.builder"
    verbose_name = _("Builder")
