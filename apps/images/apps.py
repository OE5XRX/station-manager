from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ImagesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.images"
    verbose_name = _("Images")
