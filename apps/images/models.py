from django.conf import settings
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _


class ImageRelease(models.Model):
    class Machine(models.TextChoices):
        QEMU = "qemux86-64", _("QEMU x86-64")
        RPI = "raspberrypi4-64", _("Raspberry Pi 4 (64-bit)")

    tag = models.CharField(_("release tag"), max_length=64)
    machine = models.CharField(_("machine"), max_length=32, choices=Machine.choices)
    s3_key = models.CharField(_("S3 object key"), max_length=512)
    sha256 = models.CharField(_("SHA-256"), max_length=64)
    cosign_bundle_s3_key = models.CharField(max_length=512, blank=True)
    size_bytes = models.BigIntegerField(_("size in bytes"))
    rootfs_s3_key = models.CharField(
        _("rootfs S3 object key"),
        max_length=512,
        blank=True,
        default="",
        help_text=_(
            "S3 key for the extracted root_a partition, bz2-compressed. "
            "Empty means this release has not been processed for OTA yet "
            "(re-import required)."
        ),
    )
    rootfs_sha256 = models.CharField(_("rootfs SHA-256"), max_length=64, blank=True, default="")
    rootfs_size_bytes = models.BigIntegerField(_("rootfs size in bytes"), null=True, blank=True)
    is_latest = models.BooleanField(_("latest for this machine"), default=False)
    imported_at = models.DateTimeField(auto_now_add=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="imported_images",
    )

    class Meta:
        verbose_name = _("image release")
        verbose_name_plural = _("image releases")
        constraints = [
            models.UniqueConstraint(fields=["tag", "machine"], name="uniq_tag_per_machine"),
            models.UniqueConstraint(
                fields=["machine"],
                condition=models.Q(is_latest=True),
                name="uniq_latest_per_machine",
            ),
        ]
        ordering = ["-imported_at"]

    def __str__(self):
        return f"{self.tag} ({self.machine})"

    @property
    def is_ota_ready(self) -> bool:
        """True iff the rootfs artifact has been extracted and uploaded.

        OTA deployments against this release are only viable when this
        returns True. Provisioning / bare-metal flash only need the
        full wic (``s3_key``), so an ``is_ota_ready == False`` release
        is still usable for those flows.
        """
        return bool(self.rootfs_s3_key)

    def save(self, *args, **kwargs):
        # Single `is_latest=True` per machine is an application-level invariant;
        # flipping older rows lives next to the write so both paths (admin UI,
        # worker, data migrations) get it for free.
        if self.is_latest:
            with transaction.atomic():
                ImageRelease.objects.filter(machine=self.machine, is_latest=True).exclude(
                    pk=self.pk
                ).update(is_latest=False)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)


class ImageImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        READY = "ready", _("Ready")
        FAILED = "failed", _("Failed")

    tag = models.CharField(_("release tag"), max_length=64)
    machine = models.CharField(_("machine"), max_length=32, choices=ImageRelease.Machine.choices)
    mark_as_latest = models.BooleanField(_("mark as latest"), default=True)
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.PENDING
    )
    error_message = models.TextField(_("error message"), blank=True)
    image_release = models.ForeignKey(
        "ImageRelease",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
        verbose_name=_("image release"),
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("requested by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("image import job")
        verbose_name_plural = _("image import jobs")
        ordering = ["-created_at"]

    def __str__(self):
        return f"import {self.tag}/{self.machine} ({self.status})"
