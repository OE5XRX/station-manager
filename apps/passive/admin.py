from django.contrib import admin

from .models import (
    AprsPacket,
    AprsSettings,
    CoverageCell,
    HandoverProfile,
    PassiveServiceConfig,
    PassiveServiceRun,
    StationPassiveConfig,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PassiveServiceConfig)
class PassiveServiceConfigAdmin(admin.ModelAdmin):
    list_display = ("service", "enabled", "idle_minutes")


class SingletonAdmin(admin.ModelAdmin):
    """``get()`` uses pk=1: exactly one row, never added twice or deleted."""

    def has_add_permission(self, request):
        return not self.model.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(HandoverProfile, SingletonAdmin)
admin.site.register(AprsSettings, SingletonAdmin)


@admin.register(StationPassiveConfig)
class StationPassiveConfigAdmin(admin.ModelAdmin):
    list_display = ("station", "enabled", "igate_callsign", "igate_ssid")


@admin.register(PassiveServiceRun)
class PassiveServiceRunAdmin(ReadOnlyAdmin):
    list_display = ("station", "service", "state", "started_at", "ended_at", "packet_count")
    list_filter = ("state", "service")


@admin.register(AprsPacket)
class AprsPacketAdmin(ReadOnlyAdmin):
    list_display = ("heard_at", "station", "source", "packet_type", "gated_at", "gate_skip")
    list_filter = ("packet_type",)
    search_fields = ("source", "raw_tnc2")
    raw_id_fields = ("station", "run")


@admin.register(CoverageCell)
class CoverageCellAdmin(ReadOnlyAdmin):
    list_display = ("station", "cell_lat", "cell_lon", "count", "last_heard")
