from django.contrib import admin

from apps.api.models import DeviceKey


@admin.register(DeviceKey)
class DeviceKeyAdmin(admin.ModelAdmin):
    list_display = ("station", "pubkey_preview", "is_active", "created_at", "last_seen")
    list_filter = ("is_active",)
    search_fields = ("station__name", "station__callsign")
    readonly_fields = ("current_public_key", "next_public_key", "created_at", "last_seen")

    @admin.display(description="Public Key")
    def pubkey_preview(self, obj):
        return f"{obj.current_public_key[:16]}..." if obj.current_public_key else "-"
