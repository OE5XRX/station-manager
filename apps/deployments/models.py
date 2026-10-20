from collections import defaultdict

from django.conf import settings
from django.db import models
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from apps.stations.models import Station


class Deployment(models.Model):
    """An OTA firmware deployment targeting one or more stations."""

    class TargetType(models.TextChoices):
        GROUP = "group", _("By Group")
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

    # The chosen *version* (tag + channel). Machine-agnostic: every target
    # station gets the ImageRelease of this version for its own machine,
    # pinned per station on ``DeploymentResult.image_release``.
    image_tag = models.CharField(_("image tag"), max_length=64)
    image_channel = models.CharField(_("image channel"), max_length=32, default="release")
    # Stations of the target that could not be served, with the reason:
    # [{"station_id", "station_name", "reason", "message"}, ...].
    skipped_stations = models.JSONField(_("skipped stations"), default=list, blank=True)
    target_type = models.CharField(
        _("target type"),
        max_length=10,
        choices=TargetType.choices,
    )
    target_group = models.ForeignKey(
        "stations.RolloutGroup",
        verbose_name=_("target group"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deployments",
    )
    # Snapshot of the group name at deploy time, so history stays readable
    # after the group is renamed or deleted (target_group is SET_NULL).
    target_group_name = models.CharField(_("target group name"), max_length=100, blank=True)
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
        return (
            f"Deployment #{self.pk} - {self.image_tag} [{self.image_channel}] "
            f"({self.get_status_display()})"
        )

    def save(self, *args, **kwargs):
        if self.target_group_id and not self.target_group_name:
            self.target_group_name = self.target_group.name
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], "target_group_name"}
        super().save(*args, **kwargs)

    @property
    def target_group_label(self) -> str:
        """Deploy-time group name; falls back to the live FK for old rows."""
        return self.target_group_name or (self.target_group.name if self.target_group else "")

    def get_target_stations(self):
        """Resolve the queryset of stations targeted by this deployment."""
        if self.target_type == self.TargetType.GROUP and self.target_group_id:
            return Station.objects.filter(rollout_group_id=self.target_group_id)
        elif self.target_type == self.TargetType.STATION and self.target_station:
            return Station.objects.filter(pk=self.target_station_id)
        elif self.target_type == self.TargetType.ALL:
            return Station.objects.all()
        return Station.objects.none()

    @property
    def progress(self):
        """Return a dict with per-status counts that sum to total."""
        return compute_progress(self.results)


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

    # Status set classifications. Centralized here so the cancel view,
    # supersession check, agent status-update guard, deployment-check
    # API, download API, and ``_check_deployment_complete`` all agree
    # on the same membership — historically each call site held its
    # own hard-coded tuple, and adding (or renaming) a status meant
    # hunting them all down. Use frozensets so accidental mutation
    # raises instead of silently drifting.

    # In-flight statuses — the agent is actively working the result.
    ACTIVE_STATUSES = frozenset(
        {
            Status.DOWNLOADING,
            Status.INSTALLING,
            Status.REBOOTING,
            Status.VERIFYING,
        }
    )

    # Statuses where the deployment is not yet finished. Either the
    # agent hasn't started (PENDING) or it's mid-flight.
    NON_TERMINAL_STATUSES = frozenset({Status.PENDING}) | ACTIVE_STATUSES

    # Final statuses — no further transitions allowed.
    TERMINAL_STATUSES = frozenset(
        {
            Status.SUCCESS,
            Status.FAILED,
            Status.ROLLED_BACK,
            Status.CANCELLED,
            Status.SUPERSEDED,
        }
    )

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
    # The image this station gets: the deployment's version resolved for the
    # station's machine. This is what the agent endpoints serve.
    image_release = models.ForeignKey(
        "images.ImageRelease",
        verbose_name=_("image release"),
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


def _progress_from_counts(status_counts: dict) -> dict:
    """Derive a progress dict from a ``{status: count}`` mapping.

    Shared by ``compute_progress`` (single-deployment) and
    ``bulk_compute_progress`` (batch).  All business logic lives here
    exactly once.
    """
    total = sum(status_counts.values())
    S = DeploymentResult.Status  # noqa: N806 - local alias for readability
    completed = status_counts.get(S.SUCCESS, 0)
    failed = status_counts.get(S.FAILED, 0) + status_counts.get(S.ROLLED_BACK, 0)
    pending = status_counts.get(S.PENDING, 0)
    in_progress = sum(
        status_counts.get(s, 0) for s in (S.DOWNLOADING, S.INSTALLING, S.REBOOTING, S.VERIFYING)
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


def compute_progress(results_qs):
    """Return a progress dict for the given DeploymentResult queryset.

    ``in_progress`` is computed by explicitly counting the active
    statuses rather than as ``total - completed - failed - pending``
    — the subtraction silently folded terminal CANCELLED and
    SUPERSEDED results into "in progress", which showed up on the
    deployment detail page and WebSocket payload as stale work.

    Accepts any queryset (or manager) over DeploymentResult so callers
    can scope it (e.g. to only accessible stations) before computing.
    """
    status_counts = {
        row["status"]: row["count"]
        for row in results_qs.values("status").annotate(count=Count("id"))
    }
    return _progress_from_counts(status_counts)


def bulk_compute_progress(results_qs, deployment_ids) -> dict:
    """Return ``{deployment_id: progress_dict}`` for all given IDs in ONE query.

    ``results_qs`` must be a (possibly scoped) queryset over
    ``DeploymentResult``.  The scope is honoured: only rows visible in
    ``results_qs`` are counted, so out-of-scope stations are excluded
    exactly as in ``compute_progress``.

    Every requested ``deployment_id`` gets an entry — deployments with
    zero accessible results receive an all-zero progress dict.
    """
    rows = (
        results_qs.filter(deployment_id__in=deployment_ids)
        .values("deployment_id", "status")
        .annotate(count=Count("id"))
    )
    per_dep: dict = defaultdict(dict)
    for row in rows:
        per_dep[row["deployment_id"]][row["status"]] = row["count"]
    return {dep_id: _progress_from_counts(per_dep.get(dep_id, {})) for dep_id in deployment_ids}
