from django.apps import AppConfig


class SsoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sso"
    label = "sso"
    verbose_name = "SSO / OIDC Provider"
