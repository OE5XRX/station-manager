from django.conf import settings
from django.db import models
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from apps.stations.models import Station, StationTag


class Deployment(models.Model):
    """An OTA firmware deployment targeting one or more stations."""

    class TargetType(models.TextChoices):
        TAG = "tag", _("By Tag")
        STATION = "station", _("Single Station")
        ALL = "all", _("All Stations")

    class Strategy(models.TextChoices):
        IMMEDIATE = "immediate", _("Immediate")
        PHASED = "phased", _("Phased")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        IN_PROGRESS = "in_progress", _("In Progress")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    image_release = models.ForeignKey(
        "images.ImageRelease",
        verbose_name=_("image release"),
        on_delete=models.PROTECT,
        related_name="deployments",
    )
    target_type = models.CharField(
        _("target type"),
        max_length=10,
        choices=TargetType.choices,
    )
    target_tag = models.ForeignKey(
        StationTag,
        verbose_name=_("target tag"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deployments",
    )
    target_station = models.ForeignKey(
        Station,
        verbose_name=_("target station"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="targeted_deployments",
    )
    strategy = models.CharField(
        _("strategy"),
        max_length=10,
        choices=Strategy.choices,
        default=Strategy.IMMEDIATE,
    )
    phase_config = models.JSONField(
        _("phase configuration"),
        default=dict,
        blank=True,
        help_text=_('e.g. {"batch_size": 2, "delay_seconds": 3600}'),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="deployments",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("deployment")
        verbose_name_plural = _("deployments")
        ordering = ["-created_at"]

    def __str__(self):
        return f"Deployment #{self.pk} - {self.image_release} ({self.get_status_display()})"

    def get_target_stations(self):
        """Resolve the queryset of stations targeted by this deployment."""
        if self.target_type == self.TargetType.TAG and self.target_tag:
            return Station.objects.filter(tags=self.target_tag)
        elif self.target_type == self.TargetType.STATION and self.target_station:
            return Station.objects.filter(pk=self.target_station_id)
        elif self.target_type == self.TargetType.ALL:
            return Station.objects.all()
        return Station.objects.none()

    @property
    def progress(self):
        """Return a dict with per-status counts that sum to total.

        ``in_progress`` is computed by explicitly counting the active
        statuses rather than as ``total - completed - failed - pending``
        — the subtraction silently folded terminal CANCELLED and
        SUPERSEDED results into "in progress", which showed up on the
        deployment detail page and WebSocket payload as stale work.
        """
        status_counts = {
            row["status"]: row["count"]
            for row in self.results.values("status").annotate(count=Count("id"))
        }
        total = sum(status_counts.values())
        S = DeploymentResult.Status  # noqa: N806 - local alias for readability
        completed = status_counts.get(S.SUCCESS, 0)
        failed = status_counts.get(S.FAILED, 0) + status_counts.get(S.ROLLED_BACK, 0)
        pending = status_counts.get(S.PENDING, 0)
        in_progress = sum(
            status_counts.get(s, 0)
            for s in (S.DOWNLOADING, S.INSTALLING, S.REBOOTING, S.VERIFYING)
        )
        cancelled = status_counts.get(S.CANCELLED, 0) + status_counts.get(S.SUPERSEDED, 0)
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "in_progress": in_progress,
            "cancelled": cancelled,
            "percentage": round((completed / total) * 100) if total else 0,
        }


class DeploymentResult(models.Model):
    """Per-station result for a deployment."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        DOWNLOADING = "downloading", _("Downloading")
        INSTALLING = "installing", _("Installing")
        REBOOTING = "rebooting", _("Rebooting")
        VERIFYING = "verifying", _("Verifying")
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")
        ROLLED_BACK = "rolled_back", _("Rolled Back")
        CANCELLED = "cancelled", _("Cancelled")
        SUPERSEDED = "superseded", _("Superseded")

    deployment = models.ForeignKey(
        Deployment,
        verbose_name=_("deployment"),
        on_delete=models.CASCADE,
        related_name="results",
    )
    station = models.ForeignKey(
        Station,
        verbose_name=_("station"),
        on_delete=models.PROTECT,
        related_name="deployment_results",
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)
    error_message = models.TextField(_("error message"), blank=True)
    previous_version = models.CharField(_("previous version"), max_length=100, blank=True)
    new_version = models.CharField(_("new version"), max_length=100, blank=True)

    class Meta:
        verbose_name = _("deployment result")
        verbose_name_plural = _("deployment results")
        unique_together = [("deployment", "station")]
        ordering = ["-pk"]
        indexes = [
            models.Index(fields=["deployment", "status"]),
            models.Index(fields=["station", "status"]),
        ]

    def __str__(self):
        return f"{self.station.name} - {self.get_status_display()}"
