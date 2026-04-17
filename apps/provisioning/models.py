import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ProvisioningJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        READY = "ready", _("Ready")
        DOWNLOADED = "downloaded", _("Downloaded")
        EXPIRED = "expired", _("Expired")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    station = models.ForeignKey(
        "stations.Station",
        on_delete=models.CASCADE,
        related_name="provisioning_jobs",
        verbose_name=_("station"),
    )
    image_release = models.ForeignKey(
        "images.ImageRelease",
        on_delete=models.PROTECT,
        related_name="provisioning_jobs",
        verbose_name=_("image release"),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error_message = models.TextField(_("error message"), blank=True)
    output_s3_key = models.CharField(_("output S3 key"), max_length=512, blank=True)
    output_size_bytes = models.BigIntegerField(_("output size in bytes"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    ready_at = models.DateTimeField(_("ready at"), null=True, blank=True)
    downloaded_at = models.DateTimeField(_("downloaded at"), null=True, blank=True)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("requested by"),
    )

    class Meta:
        verbose_name = _("provisioning job")
        verbose_name_plural = _("provisioning jobs")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.station.name} / {self.image_release.tag} ({self.status})"
