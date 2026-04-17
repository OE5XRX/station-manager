import hashlib

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class FirmwareArtifact(models.Model):
    """A firmware binary or OS image artifact."""

    class ArtifactType(models.TextChoices):
        OS_IMAGE = "os_image", _("OS Image")
        MODULE_FIRMWARE = "module_firmware", _("Module Firmware")

    name = models.CharField(_("name"), max_length=200)
    version = models.CharField(_("version"), max_length=50)
    artifact_type = models.CharField(
        _("artifact type"),
        max_length=20,
        choices=ArtifactType.choices,
        default=ArtifactType.OS_IMAGE,
    )
    target_module = models.ForeignKey(
        "stations.ModuleType",
        verbose_name=_("target module"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="firmware_artifacts",
    )
    file = models.FileField(
        _("file"),
        upload_to="firmware/artifacts/%Y/%m/",
    )
    file_size = models.PositiveBigIntegerField(
        _("file size"),
        editable=False,
        default=0,
    )
    checksum_sha256 = models.CharField(
        _("SHA-256 checksum"),
        max_length=64,
        editable=False,
        default="",
    )
    release_notes = models.TextField(_("release notes"), blank=True)
    is_stable = models.BooleanField(_("stable release"), default=False)
    compatible_hw_revisions = models.CharField(
        _("compatible hardware revisions"),
        max_length=500,
        blank=True,
        help_text=_("Comma-separated list of compatible hardware revisions."),
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("uploaded by"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_firmware",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("name", "version")]
        verbose_name = _("firmware artifact")
        verbose_name_plural = _("firmware artifacts")

    def __str__(self):
        return f"{self.name} v{self.version}"

    def save(self, *args, **kwargs):
        if self.file:
            # Calculate file size
            self.file.seek(0)
            self.file_size = self.file.size

            # Calculate SHA-256 checksum
            sha256 = hashlib.sha256()
            self.file.seek(0)
            for chunk in self.file.chunks():
                sha256.update(chunk)
            self.checksum_sha256 = sha256.hexdigest()
            self.file.seek(0)

        super().save(*args, **kwargs)

    @property
    def file_size_display(self):
        """Return human-readable file size."""
        size = self.file_size
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        else:
            return f"{size / (1024 * 1024 * 1024):.2f} GB"


class FirmwareDelta(models.Model):
    """A delta patch between two firmware artifacts (xdelta3)."""

    source_artifact = models.ForeignKey(
        FirmwareArtifact,
        verbose_name=_("source artifact"),
        on_delete=models.CASCADE,
        related_name="deltas_as_source",
    )
    target_artifact = models.ForeignKey(
        FirmwareArtifact,
        verbose_name=_("target artifact"),
        on_delete=models.CASCADE,
        related_name="deltas_as_target",
    )
    delta_file = models.FileField(
        _("delta file"),
        upload_to="firmware/deltas/%Y/%m/",
    )
    delta_size = models.PositiveBigIntegerField(
        _("delta size"),
        editable=False,
    )
    checksum_sha256 = models.CharField(
        _("SHA-256 checksum"),
        max_length=64,
        editable=False,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        unique_together = [("source_artifact", "target_artifact")]
        verbose_name = _("firmware delta")
        verbose_name_plural = _("firmware deltas")

    def __str__(self):
        return f"Delta: {self.source_artifact.version} \u2192 {self.target_artifact.version}"

    @property
    def delta_size_display(self):
        """Return human-readable delta size."""
        size = self.delta_size
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        else:
            return f"{size / (1024 * 1024 * 1024):.2f} GB"
