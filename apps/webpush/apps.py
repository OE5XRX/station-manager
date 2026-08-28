from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class WebpushConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.webpush"
    verbose_name = _("Web-Push")
