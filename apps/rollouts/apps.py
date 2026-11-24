from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RolloutsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.rollouts"
    verbose_name = _("Rollouts")

    def ready(self):
        import apps.rollouts.signals  # noqa: F401 — registers post_delete handlers
