from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import (
    ModuleType,
    Station,
    StationAuditLog,
    StationInventory,
    StationLogEntry,
    StationPhoto,
    StationTag,
)


class StationPhotoInline(admin.TabularInline):
    model = StationPhoto
    extra = 0
    readonly_fields = ("uploaded_at",)


class StationLogEntryInline(admin.TabularInline):
    model = StationLogEntry
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "callsign",
        "status",
        "last_seen",
        "current_os_version",
    )
    list_filter = ("status", "tags")
    search_fields = ("name", "callsign", "location_name")
    readonly_fields = (
        "last_seen",
        "last_ip_address",
        "current_os_version",
        "current_agent_version",
        "created_at",
        "updated_at",
    )
    filter_horizontal = ("installed_modules", "tags")
    inlines = [StationPhotoInline, StationLogEntryInline]
    fieldsets = (
        (None, {"fields": ("name", "callsign", "description", "tags", "notes")}),
        (
            _("Location"),
            {
                "fields": (
                    "location_name",
                    "latitude",
                    "longitude",
                    "altitude",
                ),
            },
        ),
        (
            _("Hardware"),
            {
                "fields": (
                    "hardware_revision",
                    "installed_modules",
                ),
            },
        ),
        (
            _("Agent State"),
            {
                "fields": (
                    "status",
                    "current_os_version",
                    "current_agent_version",
                    "last_ip_address",
                    "last_seen",
                ),
            },
        ),
        (
            _("Timestamps"),
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )


@admin.register(StationTag)
class StationTagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "color", "created_at")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)


@admin.register(ModuleType)
class ModuleTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "firmware_flash_method")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)


@admin.register(StationPhoto)
class StationPhotoAdmin(admin.ModelAdmin):
    list_display = ("station", "caption", "uploaded_by", "uploaded_at")
    list_filter = ("station",)
    readonly_fields = ("uploaded_at",)


@admin.register(StationLogEntry)
class StationLogEntryAdmin(admin.ModelAdmin):
    list_display = ("station", "entry_type", "title", "created_by", "created_at")
    list_filter = ("entry_type", "station")
    search_fields = ("title", "message")
    readonly_fields = ("created_at",)


@admin.register(StationInventory)
class StationInventoryAdmin(admin.ModelAdmin):
    list_display = ("station", "updated_at")
    list_filter = ("station",)
    readonly_fields = ("station", "data", "updated_at")


@admin.register(StationAuditLog)
class StationAuditLogAdmin(admin.ModelAdmin):
    list_display = ("station", "event_type", "message", "user", "created_at")
    list_filter = ("event_type", "station")
    search_fields = ("message",)
    readonly_fields = (
        "station",
        "event_type",
        "message",
        "changes",
        "user",
        "ip_address",
        "created_at",
    )
