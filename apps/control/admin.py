# apps/control/admin.py
from django.contrib import admin

from .models import ControlLock, PersistedCapability, StationModule


@admin.register(StationModule)
class StationModuleAdmin(admin.ModelAdmin):
    list_display = ("station", "slot", "module_id", "type", "online", "last_seen")
    list_filter = ("online", "type")
    search_fields = ("module_id", "type", "model")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ControlLock)
class ControlLockAdmin(admin.ModelAdmin):
    list_display = ("station", "scope", "holder", "acquired_at", "last_activity")
    list_filter = ("scope",)


@admin.register(PersistedCapability)
class PersistedCapabilityAdmin(admin.ModelAdmin):
    """Read-only: rows are written only by the gated control path (write_role +
    descriptor type-check); editing here would bypass both."""

    list_display = (
        "station",
        "slot",
        "module_id",
        "capability",
        "value",
        "updated_by",
        "updated_at",
    )
    list_filter = ("capability",)
    search_fields = ("module_id", "capability")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
