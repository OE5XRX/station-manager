from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

QUARANTINE_ATTEMPT_LIMIT = 3


class ModuleType(models.Model):
    """Registry of flashable module types (only firmware-bearing types)."""

    key = models.SlugField(_("key"), unique=True, help_text=_("z. B. fm, power, device-tester"))
    display_name = models.CharField(_("display name"), max_length=128)
    hw_repo = models.CharField(_("hardware repo"), max_length=200, blank=True)
    firmware_repo = models.CharField(
        _("firmware repo"),
        max_length=200,
        blank=True,
        help_text=_("GitHub owner/repo der signierten FW-Releases, z. B. OE5XRX/FW-RemoteStation"),
    )
    release_asset_prefix = models.CharField(
        _("release asset prefix"),
        max_length=64,
        blank=True,
        help_text=_("Asset-Basisname vor -<variant>.signed.bin, z. B. fm-sa818"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("module type")
        verbose_name_plural = _("module types")
        ordering = ["key"]

    def __str__(self):
        return self.display_name or self.key


class Module(models.Model):
    """A physical, UID-tracked module. UID is the sole identity."""

    class UidSource(models.TextChoices):
        STM32_UID = "stm32_uid", _("STM32 UID")
        SYNTHETIC = "synthetic", _("Synthetic (Sim)")

    class Lifecycle(models.TextChoices):
        READY = "ready", _("Ready")
        DEPLOYED = "deployed", _("Deployed")
        DEFECT = "defect", _("Defect")
        IN_LAB = "in_lab", _("In Lab")
        RETIRED = "retired", _("Retired")

    class Registration(models.TextChoices):
        UNREGISTERED = "unregistered", _("Unregistered")
        REGISTERED = "registered", _("Registered")

    class Convergence(models.TextChoices):
        OK = "ok", _("OK")
        UPDATING = "updating", _("Updating")
        QUARANTINED = "quarantined", _("Quarantined")
        UNKNOWN = "unknown", _("Unknown")

    # Lifecycle states that are operator-set and never auto-overridden by ingestion.
    STICKY_LIFECYCLE = {Lifecycle.DEFECT, Lifecycle.IN_LAB, Lifecycle.RETIRED}

    uid = models.CharField(_("UID"), max_length=128, unique=True)
    uid_source = models.CharField(
        _("UID source"), max_length=16, choices=UidSource.choices, default=UidSource.STM32_UID
    )
    module_type = models.ForeignKey(
        ModuleType, verbose_name=_("module type"), on_delete=models.PROTECT, related_name="modules"
    )
    lifecycle_status = models.CharField(
        _("lifecycle"), max_length=16, choices=Lifecycle.choices, default=Lifecycle.READY
    )
    registration_status = models.CharField(
        _("registration"),
        max_length=16,
        choices=Registration.choices,
        default=Registration.UNREGISTERED,
    )
    last_reported_version = models.CharField(_("last reported version"), max_length=64, blank=True)
    variant = models.CharField(_("variant"), max_length=32, blank=True, default="")
    firmware_convergence = models.CharField(
        _("firmware convergence"),
        max_length=16,
        choices=Convergence.choices,
        default=Convergence.UNKNOWN,
    )
    first_seen = models.DateTimeField(_("first seen"), null=True, blank=True)
    last_seen = models.DateTimeField(_("last seen"), null=True, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("module")
        verbose_name_plural = _("modules")
        ordering = ["module_type", "uid"]
        indexes = [
            # uid already has a unique index from unique=True — no extra index here.
            models.Index(fields=["registration_status"]),
            models.Index(fields=["lifecycle_status"]),
        ]

    def __str__(self):
        return f"{self.module_type.key}:{self.uid}"


class ModuleAssignmentHistory(models.Model):
    """Temporal module <-> (station, slot) assignment log. Open row = current."""

    module = models.ForeignKey(
        Module, verbose_name=_("module"), on_delete=models.CASCADE, related_name="assignments"
    )
    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="module_assignments",
    )
    slot = models.CharField(_("slot"), max_length=64)
    from_ts = models.DateTimeField(_("from"), default=timezone.now)
    to_ts = models.DateTimeField(_("to"), null=True, blank=True)
    reason = models.CharField(_("reason"), max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = _("module assignment")
        verbose_name_plural = _("module assignments")
        ordering = ["-from_ts"]
        constraints = [
            models.UniqueConstraint(
                fields=["module"],
                condition=models.Q(to_ts__isnull=True),
                name="uniq_open_assignment_per_module",
            ),
            models.UniqueConstraint(
                fields=["station", "slot"],
                condition=models.Q(to_ts__isnull=True),
                name="uniq_open_assignment_per_station_slot",
            ),
        ]

    def __str__(self):
        return f"{self.module.uid} @ {self.station_id}/{self.slot}"


class ModuleFirmwareReleaseManager(models.Manager):
    """Default manager hides archived (soft-deleted) rows."""

    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)


class ModuleFirmwareRelease(models.Model):
    module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.PROTECT,
        related_name="firmware_releases",
        verbose_name=_("module type"),
    )
    variant = models.CharField(_("variant"), max_length=32, blank=True)
    version = models.CharField(_("version"), max_length=64)
    storage_key = models.CharField(_("storage key"), max_length=512)
    sha256 = models.CharField(_("SHA-256"), max_length=64)
    size_bytes = models.BigIntegerField(_("size in bytes"))
    cosign_bundle_key = models.CharField(_("cosign bundle key"), max_length=512, blank=True)
    source_repo = models.CharField(_("source repo"), max_length=200)
    source_tag = models.CharField(_("source tag"), max_length=64)
    source_github_url = models.CharField(_("source URL"), max_length=512, blank=True)
    imported_at = models.DateTimeField(_("imported at"), auto_now_add=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("imported by"),
    )
    archived_at = models.DateTimeField(_("archived at"), null=True, blank=True)

    objects = ModuleFirmwareReleaseManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("module firmware release")
        verbose_name_plural = _("module firmware releases")
        base_manager_name = "all_objects"
        ordering = ["module_type", "variant", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["module_type", "variant", "version"],
                name="uniq_release_per_type_variant_version",
            ),
        ]
        indexes = [models.Index(fields=["module_type", "variant"])]

    def __str__(self):
        return f"{self.module_type.key}/{self.variant or '-'} {self.version}"

    def archive(self):
        if self.archived_at is not None:
            return
        now = timezone.now()
        with transaction.atomic():
            rows = (
                type(self)
                .all_objects.filter(pk=self.pk, archived_at__isnull=True)
                .update(archived_at=now)
            )
            if rows == 0:
                self.refresh_from_db(fields=["archived_at"])
                return
            self.archived_at = now

    def restore(self):
        if self.archived_at is None:
            return
        self.archived_at = None
        self.save(update_fields=["archived_at"])


class ModuleFirmwareImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        READY = "ready", _("Ready")
        FAILED = "failed", _("Failed")

    module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.CASCADE,
        related_name="firmware_import_jobs",
        verbose_name=_("module type"),
    )
    source_repo = models.CharField(_("source repo"), max_length=200)
    tag = models.CharField(_("release tag"), max_length=64)
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.PENDING
    )
    error_message = models.TextField(_("error message"), blank=True)
    release = models.ForeignKey(
        "ModuleFirmwareRelease",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
        verbose_name=_("release"),
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("requested by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("module firmware import job")
        verbose_name_plural = _("module firmware import jobs")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.module_type.key} {self.tag} [{self.status}]"


class ModuleFirmwareTarget(models.Model):
    """Declarative desired firmware version per module_type, with scope
    precedence station > tag > fleet and an optional canary gate."""

    class Scope(models.TextChoices):
        FLEET = "fleet", _("Fleet default")
        TAG = "tag", _("Tag override")
        STATION = "station", _("Station override")

    module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.PROTECT,
        related_name="firmware_targets",
        verbose_name=_("module type"),
    )
    version = models.CharField(_("version"), max_length=64)
    scope = models.CharField(_("scope"), max_length=16, choices=Scope.choices, default=Scope.FLEET)
    tag = models.ForeignKey(
        "stations.StationTag",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="firmware_targets",
        verbose_name=_("tag"),
    )
    station = models.ForeignKey(
        "stations.Station",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="firmware_targets",
        verbose_name=_("station"),
    )
    canary_tag = models.ForeignKey(
        "stations.StationTag",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("canary tag"),
        help_text=_("While set, a fleet target applies only to stations in this tag."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("module firmware target")
        verbose_name_plural = _("module firmware targets")
        ordering = ["module_type", "scope"]
        constraints = [
            models.UniqueConstraint(
                fields=["module_type"],
                condition=models.Q(scope="fleet"),
                name="uniq_fleet_target_per_type",
            ),
            models.UniqueConstraint(
                fields=["module_type", "tag"],
                condition=models.Q(scope="tag"),
                name="uniq_tag_target_per_type_tag",
            ),
            models.UniqueConstraint(
                fields=["module_type", "station"],
                condition=models.Q(scope="station"),
                name="uniq_station_target_per_type_station",
            ),
            # Scope/ref coherence: nullable tag/station must match the scope, so
            # effective_target can never misbehave on an inconsistent row.
            models.CheckConstraint(
                condition=(
                    ~models.Q(scope="fleet")
                    | (models.Q(tag__isnull=True) & models.Q(station__isnull=True))
                ),
                name="target_fleet_has_no_ref",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(scope="tag")
                    | (models.Q(tag__isnull=False) & models.Q(station__isnull=True))
                ),
                name="target_tag_has_tag_only",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(scope="station")
                    | (models.Q(station__isnull=False) & models.Q(tag__isnull=True))
                ),
                name="target_station_has_station_only",
            ),
            # canary_tag is a fleet-only gate; effective_target ignores it on
            # tag/station scopes, so allowing it there is silent dead intent.
            models.CheckConstraint(
                condition=(models.Q(scope="fleet") | models.Q(canary_tag__isnull=True)),
                name="canary_tag_only_on_fleet",
            ),
        ]

    def clean(self):
        """Form-level mirror of the scope/ref CheckConstraints."""
        super().clean()
        from django.core.exceptions import ValidationError

        if self.scope == self.Scope.FLEET:
            if self.tag_id is not None or self.station_id is not None:
                raise ValidationError(_("A fleet target must not set a tag or station."))
        elif self.scope == self.Scope.TAG:
            if self.tag_id is None or self.station_id is not None:
                raise ValidationError(_("A tag target must set a tag and no station."))
        elif self.scope == self.Scope.STATION:
            if self.station_id is None or self.tag_id is not None:
                raise ValidationError(_("A station target must set a station and no tag."))
        if self.scope != self.Scope.FLEET and self.canary_tag_id is not None:
            raise ValidationError(_("A canary tag is only allowed on a fleet target."))

    def __str__(self):
        return f"{self.module_type.key} {self.scope}={self.version}"


class ModuleFirmwareConvergenceState(models.Model):
    """Reconciler bookkeeping per (module, target release): attempts,
    quarantine, last error mode. Quarantine binds to the tuple, so a new
    target version is a new row and is retried automatically."""

    class State(models.TextChoices):
        OK = "ok", _("OK")
        UPDATING = "updating", _("Updating")
        QUARANTINED = "quarantined", _("Quarantined")

    class ErrorMode(models.TextChoices):
        REJECTED = "rejected", _("Rejected")
        ROLLED_BACK = "rolled_back", _("Rolled back")
        TRANSIENT = "transient", _("Transient")

    module = models.ForeignKey(
        Module,
        on_delete=models.CASCADE,
        related_name="convergence_states",
        verbose_name=_("module"),
    )
    target_release = models.ForeignKey(
        ModuleFirmwareRelease,
        on_delete=models.PROTECT,
        related_name="convergence_states",
        verbose_name=_("target release"),
    )
    state = models.CharField(
        _("state"), max_length=16, choices=State.choices, default=State.UPDATING
    )
    attempts = models.PositiveIntegerField(_("attempts"), default=0)
    last_error_mode = models.CharField(
        _("last error mode"), max_length=16, choices=ErrorMode.choices, blank=True, default=""
    )
    last_error_message = models.TextField(_("last error message"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    last_attempt_at = models.DateTimeField(_("last attempt at"), null=True, blank=True)

    class Meta:
        verbose_name = _("module firmware convergence state")
        verbose_name_plural = _("module firmware convergence states")
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["module", "target_release"],
                name="uniq_convergence_per_module_release",
            ),
        ]

    def __str__(self):
        return f"{self.module.uid} -> {self.target_release.version} [{self.state}]"
