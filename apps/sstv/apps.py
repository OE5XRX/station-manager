from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SstvConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sstv"
    label = "sstv"
    verbose_name = _("SSTV")

    def ready(self):
        from apps.control import lock, lock_hooks

        from . import (
            control,
            signals,  # noqa: F401 — deletes stored files with their rows
        )

        # Every lock change ends SSTV (spec §5.2); a running session keeps the lock busy.
        lock_hooks.register_release_hook(control.on_lock_released)
        lock_hooks.register_acquire_hook(control.on_lock_acquired)
        lock.register_activity_check(control.counts_as_activity)
