"""UI forms for threshold rules and metric definitions (spec §6).

Widgets are rendered by hand in the templates (dot-decimal ``|unlocalize``,
``lang="en"``), so these forms only carry fields + validation.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import AlertRule, MetricDefinition
from .rules import validate_metric_key


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
        self._persisted_threshold_key = (
            self.instance.metric_key
            if self.instance.pk and self.instance.alert_type == AlertRule.AlertType.THRESHOLD
            else None
        )
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
        # Mirror AlertRuleWriteSerializer: only (re)validate a changed key —
        # seeded rules (e.g. the 12 V rule) may reference keys whose definition
        # only appears once an agent describes it; editing them must not block.
        if value and value == self._persisted_threshold_key:
            return value
        validate_metric_key(value)
        return value


class MetricDefinitionForm(forms.ModelForm):
    class Meta:
        model = MetricDefinition
        fields = ["label_override", "min_override", "max_override", "is_hidden", "is_blocked"]

    def clean(self):
        cleaned = super().clean()
        lo, hi = cleaned.get("min_override"), cleaned.get("max_override")
        if lo is not None and hi is not None and lo > hi:
            raise forms.ValidationError(
                _("Minimum override must not be greater than the maximum override."),
                code="range_inverted",
            )
        return cleaned
