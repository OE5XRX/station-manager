from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.stations.models import Station


class AlertRule(models.Model):
    """Configurable rule that defines when an alert should be triggered."""

    class AlertType(models.TextChoices):
        STATION_OFFLINE = "station_offline", _("Station Offline")
        CPU_TEMPERATURE = "cpu_temperature", _("CPU Temperature")
        DISK_WARNING = "disk_warning", _("Disk Warning")
        DISK_CRITICAL = "disk_critical", _("Disk Critical")
        RAM_CRITICAL = "ram_critical", _("RAM Critical")
        OTA_FAILED = "ota_failed", _("OTA Failed")

    class Severity(models.TextChoices):
        WARNING = "warning", _("Warning")
        CRITICAL = "critical", _("Critical")

    alert_type = models.CharField(
        _("alert type"),
        max_length=20,
        choices=AlertType.choices,
        unique=True,
    )
    threshold = models.FloatField(
        _("threshold"),
        help_text=_("e.g. 80.0 for CPU temp, 90.0 for disk %, 90.0 for RAM %"),
    )
    severity = models.CharField(
        _("severity"),
        max_length=10,
        choices=Severity.choices,
    )
    is_active = models.BooleanField(_("active"), default=True)
    description = models.CharField(_("description"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("alert rule")
        verbose_name_plural = _("alert rules")
        ordering = ["alert_type"]

    def __str__(self):
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
