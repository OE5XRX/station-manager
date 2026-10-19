from django.contrib import admin
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.accounts.client_ip import client_ip

from .models import (
    RolloutGroup,
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
    list_filter = ("status", "rollout_group", "tags")
    search_fields = ("name", "callsign", "location_name")
    readonly_fields = (
        "last_seen",
        "last_ip_address",
        "current_os_version",
        "current_agent_version",
        "created_at",
        "updated_at",
        # Group changes must go through the audited station-edit path
        # (StationAuditLog ROLLOUT_GROUP_CHANGED); admin shows it read-only.
        "rollout_group",
    )
    filter_horizontal = ("tags",)
    inlines = [StationPhotoInline, StationLogEntryInline]
    fieldsets = (
        (None, {"fields": ("name", "callsign", "description", "rollout_group", "tags", "notes")}),
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
                "fields": ("hardware_revision",),
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


@admin.register(RolloutGroup)
class RolloutGroupAdmin(admin.ModelAdmin):
    """Same rules as the UI/API: channel fixed after create, every write
    audited (CONFIG_CHANGED); PROTECT FKs block deleting a referenced group."""

    list_display = ("name", "slug", "channel", "created_at")
    list_filter = ("channel",)
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)

    def get_readonly_fields(self, request, obj=None):
        return ("channel",) if obj else ()

    def _audit(self, request, obj, action, before=None):
        from apps.rollouts.services import audit_group

        audit_group(
            obj,
            action,
            actor=request.user,
            ip_address=client_ip(request),
            suffix=f" by {request.user} (admin)",
            before=before,
        )

    def save_model(self, request, obj, form, change):
        from apps.rollouts.services import group_snapshot

        before = group_snapshot(RolloutGroup.objects.get(pk=obj.pk)) if change else None
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            self._audit(request, obj, "updated" if change else "created", before)

    def delete_model(self, request, obj):
        with transaction.atomic():
            self._audit(request, obj, "deleted")
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            for obj in queryset:
                self._audit(request, obj, "deleted")
            super().delete_queryset(request, queryset)


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

    def has_add_permission(self, request):
        return False  # audit log entries only created programmatically

    def has_change_permission(self, request, obj=None):
        return False  # audit log is append-only

    def has_delete_permission(self, request, obj=None):
        return False  # append-only: even superusers must not delete
