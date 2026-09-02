from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ImageReleaseManager(models.Manager):
    """Default manager: hides archived (soft-deleted) rows.

    Use ``ImageRelease.all_objects`` to get the full set (incl.
    archived) — e.g. the "Show archived" UI toggle, auto-restore
    lookups during re-import, Django admin.
    """

    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)


class ImageRelease(models.Model):
    class Machine(models.TextChoices):
        QEMU = "qemux86-64", _("QEMU x86-64")
        RPI = "raspberrypi4-64", _("Raspberry Pi 4 (64-bit)")

    tag = models.CharField(_("release tag"), max_length=64)
    machine = models.CharField(_("machine"), max_length=32, choices=Machine.choices)
    channel = models.CharField(
        _("channel"),
        max_length=32,
        default="release",
        help_text=_(
            "Image variant/channel, baked at build time (release, dev, …). "
            "Free-form slug; no governance is enforced on it."
        ),
    )
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
    # Soft-delete timestamp. Use the archive() / restore() methods
    # rather than setting this field directly — archive() also clears
    # is_latest to preserve the "latest archived row cannot exist"
    # invariant that the rest of the codebase relies on.
    archived_at = models.DateTimeField(
        _("archived at"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "Soft-delete timestamp. Archived releases are hidden from "
            "the default UI list but remain available for any "
            "Deployment or ProvisioningJob that still references them."
        ),
    )

    objects = ImageReleaseManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("image release")
        verbose_name_plural = _("image releases")
        # FK dereference (Deployment.image_release, ProvisioningJob.
        # image_release, ImageImportJob.image_release) goes through the
        # *base* manager, which defaults to the FIRST declared manager —
        # that's our filtering ImageReleaseManager. Without this override
        # archiving a release would silently break every related-object
        # access pointing at it: deployment.image_release becomes None
        # mid-flight, the agent's deployment-check 500s, the audit trail
        # disappears from the UI. Pin the base manager to all_objects so
        # related lookups always see the row regardless of archived_at;
        # default_manager_name (= objects) keeps the UI list and KPI
        # counts honest.
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=["tag", "machine", "channel"],
                name="uniq_tag_per_machine_channel",
            ),
            models.UniqueConstraint(
                fields=["machine", "channel"],
                condition=models.Q(is_latest=True),
                name="uniq_latest_per_machine_channel",
            ),
        ]
        ordering = ["-imported_at"]

    def __str__(self):
        return f"{self.tag} ({self.machine})"

    @property
    def is_ota_ready(self) -> bool:
        """True iff the rootfs artifact and required OTA metadata exist.

        OTA deployments against this release are only viable when the
        extracted rootfs has been uploaded and its checksum and size are
        available. Provisioning / bare-metal flash only need the full
        wic (``s3_key``), so an ``is_ota_ready == False`` release is
        still usable for those flows.
        """
        return bool(
            self.rootfs_s3_key
            and self.rootfs_sha256
            and self.rootfs_size_bytes
            and self.rootfs_size_bytes > 0
        )

    def save(self, *args, **kwargs):
        # Single `is_latest=True` per machine is an application-level invariant;
        # flipping older rows lives next to the write so both paths (admin UI,
        # worker, data migrations) get it for free.
        #
        # all_objects rather than objects: if an archived row ever ended
        # up with is_latest=True (handcrafted URL, bad data fixup, old
        # migration), the default manager would hide it and a later
        # mark-latest on another release would hit the
        # uniq_latest_per_machine DB constraint. Defence in depth for the
        # "single latest per machine" invariant.
        if self.is_latest:
            with transaction.atomic():
                ImageRelease.all_objects.filter(
                    machine=self.machine, channel=self.channel, is_latest=True
                ).exclude(pk=self.pk).update(is_latest=False)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def archive(self):
        """Soft-delete this release. Idempotent under concurrency.

        Atomically stamps ``archived_at`` and clears ``is_latest`` —
        a "latest archived" row would be semantically nonsensical and
        would also stop the partial unique index from doing useful
        work on the next ``mark_as_latest`` operation.

        Concurrency: the in-memory ``self.archived_at`` may be stale
        (two concurrent POSTs can both observe ``None`` before either
        commits). The write is therefore guarded by a conditional
        UPDATE WHERE archived_at IS NULL — the DB arbitrates, only
        the first transaction's update touches the row, and we use
        the rowcount to skip the in-memory mutation on the loser.
        """
        if self.archived_at is not None:
            return  # short-circuit: already known archived in-memory

        now = timezone.now()
        with transaction.atomic():
            rows = (
                type(self)
                .all_objects.filter(pk=self.pk, archived_at__isnull=True)
                .update(archived_at=now, is_latest=False)
            )
            if rows == 0:
                # Another transaction archived this row first; refresh
                # in-memory state and return — idempotent no-op.
                self.refresh_from_db(fields=["archived_at", "is_latest"])
                return
            self.archived_at = now
            self.is_latest = False

    def restore(self):
        """Undo a previous archive. Idempotent.

        Does NOT touch ``is_latest`` — re-promotion to "latest" after
        a restore is an explicit operator action via the existing
        Mark-Latest button. Restoring a previously-archived release
        must not silently steal the latest bit from whatever is
        currently active.
        """
        if self.archived_at is None:
            return  # idempotent

        self.archived_at = None
        self.save(update_fields=["archived_at"])


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
