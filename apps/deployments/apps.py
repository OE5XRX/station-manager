from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DeploymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.deployments"
    verbose_name = _("Deployments")
