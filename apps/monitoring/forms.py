"""UI forms for threshold rules and metric definitions (spec §6).

Widgets are rendered by hand in the templates (dot-decimal ``|unlocalize``,
``lang="en"``), so these forms only carry fields + validation.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import AlertRule, MetricDefinition
from .rules import inverted_override_field, persisted_threshold_key, validate_changed_metric_key


class ThresholdRuleForm(forms.ModelForm):
    class Meta:
        model = AlertRule
        fields = [
            "name",
            "metric_key",
            "operator",
            "aggregate",
            "threshold",
            "duration_seconds",
            "severity",
            "is_active",
            "description",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Captured before alert_type is forced below: the persisted state decides
        # whether an unchanged metric key may skip re-validation.
        self._persisted_threshold_key = persisted_threshold_key(self.instance)
        # Forced, never from POST: the form only ever edits threshold rules
        # (set before validation so the event-type constraint check is exact).
        self.instance.alert_type = AlertRule.AlertType.THRESHOLD
        # blank=True on the model (event rules leave it empty) — required here.
        self.fields["metric_key"].required = True
        self.fields["operator"].required = True
        self.fields["operator"].choices = AlertRule.Operator.choices
        self.fields["aggregate"].choices = AlertRule.Aggregate.choices

    def clean_metric_key(self):
        value = (self.cleaned_data.get("metric_key") or "").strip()
        # Mirror AlertRuleWriteSerializer: only (re)validate a changed key.
        validate_changed_metric_key(value, self._persisted_threshold_key)
        return value


class MetricDefinitionForm(forms.ModelForm):
    class Meta:
        model = MetricDefinition
        fields = ["label_override", "min_override", "max_override", "is_hidden", "is_blocked"]

    def clean(self):
        cleaned = super().clean()
        # self.instance still holds the persisted agent range here
        # (construct_instance runs in _post_clean, after clean()).
        field = inverted_override_field(
            cleaned.get("min_override"),
            cleaned.get("max_override"),
            self.instance.min_value,
            self.instance.max_value,
        )
        if field is not None:
            self.add_error(
                field,
                forms.ValidationError(
                    _("The effective minimum must not be greater than the effective maximum."),
                    code="range_inverted",
                ),
            )
        return cleaned


class AlertRuleAdminForm(forms.ModelForm):
    """Django-admin form: threshold rules get the same operator / metric-key
    checks as the UI form and the API serializer; event rules are untouched."""

    class Meta:
        model = AlertRule
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Before construct_instance (in _post_clean) overwrites the instance.
        self._persisted_threshold_key = persisted_threshold_key(self.instance)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("alert_type") != AlertRule.AlertType.THRESHOLD:
            return cleaned
        if not cleaned.get("operator"):
            self.add_error(
                "operator",
                forms.ValidationError(_("Threshold rules require an operator."), code="required"),
            )
        if "metric_key" in cleaned:
            key = (cleaned.get("metric_key") or "").strip()
            cleaned["metric_key"] = key
            try:
                validate_changed_metric_key(key, self._persisted_threshold_key)
            except forms.ValidationError as exc:
                self.add_error("metric_key", exc)
        return cleaned
