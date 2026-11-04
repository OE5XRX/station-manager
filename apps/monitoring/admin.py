from django.contrib import admin

from .forms import AlertRuleAdminForm, MetricDefinitionForm
from .models import Alert, AlertRule, MetricDefinition, ThresholdState


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    form = AlertRuleAdminForm
    list_display = (
        "alert_type",
        "name",
        "metric_key",
        "operator",
        "threshold",
        "aggregate",
        "duration_seconds",
        "severity",
        "is_active",
        "created_at",
    )
    list_filter = ("alert_type", "severity", "is_active")
    list_editable = ("threshold", "is_active")
    search_fields = ("name", "metric_key", "description")


@admin.register(MetricDefinition)
class MetricDefinitionAdmin(admin.ModelAdmin):
    # Effective-range (inverted override) + printable-label validation, as in the UI.
    form = MetricDefinitionForm
    list_display = (
        "key",
        "label",
        "label_override",
        "unit",
        "group",
        "is_hidden",
        "is_blocked",
        "last_seen",
    )
    list_filter = ("group", "is_hidden", "is_blocked")
    list_editable = ("is_hidden", "is_blocked")
    search_fields = ("key", "label", "label_override")
    # Agent fields belong to the heartbeat ingest (last writer wins); the
    # admin only edits the operator fields.
    readonly_fields = (
        "key",
        "label",
        "unit",
        "group",
        "precision",
        "min_value",
        "max_value",
        "source_agent_version",
        "first_seen",
        "last_seen",
        "last_rejected_at",
        "last_rejected_value",
    )

    def has_add_permission(self, request):
        return False  # rows are created by the heartbeat ingest only

    def save_model(self, request, obj, form, change):
        if change:
            # Only the operator fields, so a concurrent heartbeat's agent-field
            # update is never overwritten. A normal save → signals still fire.
            obj.save(update_fields=list(MetricDefinition.OPERATOR_FIELDS))
        else:
            super().save_model(request, obj, form, change)


@admin.register(ThresholdState)
class ThresholdStateAdmin(admin.ModelAdmin):
    list_display = (
        "rule",
        "station",
        "metric_key",
        "breach_since",
        "ok_since",
        "last_evaluated_at",
        "alert",
    )
    list_filter = ("rule",)
    list_select_related = ("rule", "station", "alert__station")
    search_fields = ("metric_key", "station__name")
    raw_id_fields = ("rule", "station", "alert")


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
