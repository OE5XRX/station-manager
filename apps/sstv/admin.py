from django.contrib import admin

from .models import ActiveSstvRx, SstvImage, SstvSettings, SstvTransmission


@admin.register(SstvSettings)
class SstvSettingsAdmin(admin.ModelAdmin):
    """``load()`` uses pk=1: exactly one row, never added twice or deleted."""

    def has_add_permission(self, request):
        return not self.model.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class _ReadOnlyAdmin(admin.ModelAdmin):
    """Rows are written by the SSTV flow only; admins may look and delete."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SstvImage)
class SstvImageAdmin(_ReadOnlyAdmin):
    list_display = ("created_at", "station", "direction", "mode", "complete", "callsign")
    list_filter = ("direction", "mode", "complete")
    list_select_related = ("station",)
    search_fields = ("station__name", "callsign")


@admin.register(SstvTransmission)
class SstvTransmissionAdmin(_ReadOnlyAdmin):
    list_display = ("created_at", "station", "mode", "state", "user")
    list_filter = ("state", "mode")
    list_select_related = ("station", "user")


@admin.register(ActiveSstvRx)
class ActiveSstvRxAdmin(_ReadOnlyAdmin):
    list_display = ("station", "slot", "state", "user", "started_at")
    list_select_related = ("station", "user")
