from django.apps import AppConfig


class SsoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sso"
    label = "sso"
    verbose_name = "SSO / OIDC Provider"

    def ready(self):
        # Import for side effects: connects the signal handlers.
        from . import signals  # noqa: F401
