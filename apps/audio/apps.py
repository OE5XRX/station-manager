from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AudioConfig(AppConfig):
    name = "apps.audio"
    verbose_name = _("Audio Relay")
