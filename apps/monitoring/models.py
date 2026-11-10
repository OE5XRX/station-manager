from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.stations.models import Station
from station_agent.metrics_contract import (
    GROUPS,
    MAX_KEY_LEN,
    MAX_LABEL_LEN,
    MAX_UNIT_LEN,
    is_display_char,
)

# Flood guard (spec §3.1): no new MetricDefinition rows beyond this count.
MAX_DEFINITIONS = 2000


def validate_printable(value):
    """Reject CR/LF/control/format characters: the label ends up in Alert.title
    and from there in the e-mail subject, where a line break raises a header
    error and the mail is lost. NBSP-style spaces stay allowed (same predicate
    as the agent-side descriptor cleaning)."""
    if any(not is_display_char(ch) for ch in value):
        raise ValidationError(
            _("Control characters (e.g. line breaks) are not allowed."), code="not_printable"
        )


class AlertRule(models.Model):
    """Configurable rule that defines when an alert should be triggered."""

    class AlertType(models.TextChoices):
        STATION_OFFLINE = "station_offline", _("Station Offline")
        OTA_FAILED = "ota_failed", _("OTA Failed")
        UNEXPECTED_REBOOT = "unexpected_reboot", _("Unexpected Reboot")
        POWER_WARNING = "power_warning", _("Power Warning")
        STORAGE_HEALTH = "storage_health", _("Storage Health")
        THRESHOLD = "threshold", _("Threshold")
        PASSIVE_NO_RX = "passive_no_rx", _("Passive RX silent")

    class Operator(models.TextChoices):
        GT = "gt", ">"
        GTE = "gte", ">="
        LT = "lt", "<"
        LTE = "lte", "<="

    class Aggregate(models.TextChoices):
        AVG = "avg", _("Average")
        MIN = "min", _("Minimum")
        MAX = "max", _("Maximum")

    class Severity(models.TextChoices):
        WARNING = "warning", _("Warning")
        CRITICAL = "critical", _("Critical")

    alert_type = models.CharField(
        _("alert type"),
        max_length=20,
        choices=AlertType.choices,
    )
    threshold = models.FloatField(
        _("threshold"),
        help_text=_(
            "Threshold rules: limit compared against the metric value (in the metric's unit). "
            "Event rules: type-specific parameter (e.g. minutes for station offline)."
        ),
    )
    severity = models.CharField(
        _("severity"),
        max_length=10,
        choices=Severity.choices,
    )
    is_active = models.BooleanField(_("active"), default=True)
    description = models.CharField(_("description"), max_length=200, blank=True)
    # Threshold-rule fields (only meaningful for alert_type == THRESHOLD).
    name = models.CharField(_("name"), max_length=100, blank=True)
    metric_key = models.CharField(
        _("metric key"),
        max_length=MAX_KEY_LEN,
        blank=True,
        help_text=_("Metric key or pattern with one '*' segment, e.g. disk.*.used_pct"),
    )
    operator = models.CharField(_("operator"), max_length=3, choices=Operator.choices, blank=True)
    aggregate = models.CharField(
        _("aggregate"), max_length=3, choices=Aggregate.choices, default=Aggregate.AVG
    )
    duration_seconds = models.PositiveIntegerField(
        _("duration (seconds)"),
        default=0,
        help_text=_(
            "Breach (and recovery) must hold this long before the alert fires (resolves)."
        ),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("alert rule")
        verbose_name_plural = _("alert rules")
        ordering = ["alert_type", "metric_key", "threshold"]
        constraints = [
            # Event types stay singletons; any number of threshold rules allowed.
            models.UniqueConstraint(
                fields=["alert_type"],
                condition=~Q(alert_type="threshold"),
                name="monitoring_alertrule_unique_event_type",
            ),
        ]

    def __str__(self):
        if self.alert_type == self.AlertType.THRESHOLD:
            return f"{self.name or self.metric_key} {self.operator} {self.threshold}"
        return f"{self.get_alert_type_display()} ({self.get_severity_display()})"


class Alert(models.Model):
    """An alert instance triggered by a rule for a specific station."""

    class Severity(models.TextChoices):
        WARNING = "warning", _("Warning")
        CRITICAL = "critical", _("Critical")

    station = models.ForeignKey(
        Station,
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="alerts",
    )
    alert_rule = models.ForeignKey(
        AlertRule,
        verbose_name=_("alert rule"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="alerts",
    )
    severity = models.CharField(
        _("severity"),
        max_length=10,
        choices=Severity.choices,
    )
    title = models.CharField(_("title"), max_length=200)
    message = models.TextField(_("message"))
    is_acknowledged = models.BooleanField(_("acknowledged"), default=False)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("acknowledged by"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acknowledged_alerts",
    )
    acknowledged_at = models.DateTimeField(_("acknowledged at"), null=True, blank=True)
    is_resolved = models.BooleanField(_("resolved"), default=False)
    resolved_at = models.DateTimeField(_("resolved at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("alert")
        verbose_name_plural = _("alerts")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["is_resolved", "severity", "created_at"]),
        ]

    def __str__(self):
        return f"{self.station.name} - {self.title}"


class MetricDefinition(models.Model):
    """Agent-described metric (one row per concrete key, global).

    Agent fields follow last-writer-wins; operator overrides always win
    (spec §3.1). Rows are never deleted automatically.
    """

    GROUP_CHOICES = [(g, g) for g in GROUPS]

    # Fields an operator may write (UI / API / admin); everything else is
    # owned by the heartbeat ingest and must never be overwritten by them.
    OPERATOR_FIELDS = ("label_override", "min_override", "max_override", "is_hidden", "is_blocked")

    # Agent-provided
    key = models.CharField(_("key"), max_length=MAX_KEY_LEN, unique=True)
    label = models.CharField(_("label"), max_length=MAX_LABEL_LEN, blank=True)
    unit = models.CharField(_("unit"), max_length=MAX_UNIT_LEN, blank=True)
    group = models.CharField(_("group"), max_length=16, choices=GROUP_CHOICES, default="other")
    precision = models.PositiveSmallIntegerField(_("precision"), default=1)
    min_value = models.FloatField(_("plausible minimum"), null=True, blank=True)
    max_value = models.FloatField(_("plausible maximum"), null=True, blank=True)
    source_agent_version = models.CharField(_("source agent version"), max_length=32, blank=True)
    first_seen = models.DateTimeField(_("first seen"), auto_now_add=True)
    last_seen = models.DateTimeField(_("last seen"), default=timezone.now)
    # Operator-managed
    label_override = models.CharField(
        _("label override"),
        max_length=MAX_LABEL_LEN,
        blank=True,
        validators=[validate_printable],
    )
    min_override = models.FloatField(_("minimum override"), null=True, blank=True)
    max_override = models.FloatField(_("maximum override"), null=True, blank=True)
    is_hidden = models.BooleanField(_("hidden"), default=False)
    is_blocked = models.BooleanField(_("blocked"), default=False)
    # Last plausibility rejection (visible in metric management)
    last_rejected_at = models.DateTimeField(_("last rejected at"), null=True, blank=True)
    last_rejected_value = models.JSONField(_("last rejected value"), null=True, blank=True)

    class Meta:
        verbose_name = _("metric definition")
        verbose_name_plural = _("metric definitions")
        ordering = ["group", "key"]

    def __str__(self):
        return self.key

    @property
    def display_label(self):
        return self.label_override or self.label or self.key

    @property
    def range_min(self):
        return self.min_override if self.min_override is not None else self.min_value

    @property
    def range_max(self):
        return self.max_override if self.max_override is not None else self.max_value


class ThresholdState(models.Model):
    """Persistent duration/hysteresis state per (rule, station, concrete metric key)."""

    rule = models.ForeignKey(
        AlertRule,
        on_delete=models.CASCADE,
        related_name="threshold_states",
        verbose_name=_("rule"),
    )
    station = models.ForeignKey(
        Station,
        on_delete=models.CASCADE,
        related_name="threshold_states",
        verbose_name=_("station"),
    )
    metric_key = models.CharField(_("metric key"), max_length=MAX_KEY_LEN)
    breach_since = models.DateTimeField(_("breach since"), null=True, blank=True)
    ok_since = models.DateTimeField(_("ok since"), null=True, blank=True)
    # Last run that saw a current value while a timer was pending; a longer
    # gap than OFFLINE_THRESHOLD restarts the pending timer (gaps never count
    # toward duration_seconds).
    last_evaluated_at = models.DateTimeField(_("last evaluated at"), null=True, blank=True)
    alert = models.ForeignKey(
        Alert,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("alert"),
    )

    class Meta:
        verbose_name = _("threshold state")
        verbose_name_plural = _("threshold states")
        unique_together = [("rule", "station", "metric_key")]

    def __str__(self):
        return f"{self.rule_id}/{self.station_id}/{self.metric_key}"
