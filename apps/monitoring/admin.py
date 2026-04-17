from django.contrib import admin

from .models import Alert, AlertRule


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ("alert_type", "threshold", "severity", "is_active", "created_at")
    list_filter = ("severity", "is_active")
    list_editable = ("threshold", "is_active")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "station",
        "severity",
        "is_acknowledged",
        "is_resolved",
        "created_at",
    )
    list_filter = ("severity", "is_acknowledged", "is_resolved", "alert_rule__alert_type")
    search_fields = ("title", "message", "station__name")
    raw_id_fields = ("station", "acknowledged_by")
    readonly_fields = ("created_at",)
