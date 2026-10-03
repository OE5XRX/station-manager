from django.apps import AppConfig


class RolloutsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.rollouts"
    verbose_name = "Rollouts"

    def ready(self):
        import apps.rollouts.signals  # noqa: F401 — registers post_delete handlers
