from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ProvisioningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.provisioning"
    verbose_name = _("Provisioning")
