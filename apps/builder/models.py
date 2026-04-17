from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class BuildConfig(models.Model):
    """A build configuration describing how to assemble a station image."""

    name = models.CharField(_("name"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="build_configs",
    )
    tag = models.ForeignKey(
        "stations.StationTag",
        verbose_name=_("tag"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="build_configs",
    )
    base_image = models.ForeignKey(
        "firmware.FirmwareArtifact",
        verbose_name=_("base image"),
        on_delete=models.PROTECT,
        related_name="build_configs_as_base",
    )
    extra_firmware = models.ManyToManyField(
        "firmware.FirmwareArtifact",
        verbose_name=_("extra firmware"),
        blank=True,
        related_name="build_configs_as_extra",
    )
    custom_config = models.JSONField(
        _("custom config"),
        default=dict,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="build_configs",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("build config")
        verbose_name_plural = _("build configs")

    def __str__(self):
        return self.name

    @property
    def target_display(self):
        """Return a human-readable description of the build target."""
        if self.station:
            return str(self.station)
        if self.tag:
            return str(self.tag)
        return "-"

    @property
    def latest_job(self):
        """Return the most recent build job, or None."""
        return self.jobs.order_by("-created_at").first()


class BuildJob(models.Model):
    """A single build execution for a BuildConfig."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        BUILDING = "building", _("Building")
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")

    build_config = models.ForeignKey(
        BuildConfig,
        verbose_name=_("build config"),
        on_delete=models.PROTECT,
        related_name="jobs",
    )
    status = models.CharField(
        _("status"),
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    output_artifact = models.ForeignKey(
        "firmware.FirmwareArtifact",
        verbose_name=_("output artifact"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="build_jobs",
    )
    log = models.TextField(_("log"), blank=True)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="build_jobs",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("build job")
        verbose_name_plural = _("build jobs")

    def __str__(self):
        return f"{self.build_config.name} #{self.pk} ({self.get_status_display()})"

    @property
    def status_badge_class(self):
        """Return the Bootstrap badge CSS class for the current status."""
        return {
            self.Status.PENDING: "bg-warning text-dark",
            self.Status.BUILDING: "bg-info text-dark",
            self.Status.SUCCESS: "bg-success",
            self.Status.FAILED: "bg-danger",
        }.get(self.status, "bg-secondary")
