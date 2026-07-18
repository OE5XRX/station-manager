# apps/control/admin.py
from django.contrib import admin

from .models import ControlLock, StationModule


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
