from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User

# Slugs reserved by the rollout machinery (apps.rollouts.grouping) —
# creating a StationTag with one of these slugs would let a real tag
# shadow the sentinel bucket used for "stations outside the sequence".
RESERVED_TAG_SLUGS = frozenset({"__unassigned__"})


class Region(models.Model):
    """A geographic / organizational grouping of stations.

    Freely manageable via /regions/ admin UI. Provides the scope for
    RegionAssignment (a Region-Manager has operative authority over
    all stations of the region). Station.region is the FK.
    """

    name = models.CharField(_("name"), max_length=80, unique=True)
    slug = models.SlugField(_("slug"), unique=True)
    description = models.TextField(_("description"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("region")
        verbose_name_plural = _("regions")
        ordering = ["name"]

    def __str__(self):
        return self.name


class StationTag(models.Model):
    """Tags for categorizing stations (e.g., 'testbetrieb', 'hamnet', 'lte', 'hw-rev-v2').

    Unlike groups, a station can have multiple tags. Tags are used for
    filtering, batch operations (OTA rollouts), and organization.
    """

    name = models.CharField(_("name"), max_length=100, unique=True)
    slug = models.SlugField(_("slug"), unique=True)
    color = models.CharField(
        _("color"),
        max_length=7,
        default="#6c757d",
        help_text=_("Hex color for the tag badge, e.g. #0d6efd"),
    )
    description = models.TextField(_("description"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("station tag")
        verbose_name_plural = _("station tags")
        ordering = ["name"]
        constraints = [
            # DB-level guard: clean() alone doesn't fire for
            # .objects.create() / bulk_create() / raw .save(), so
            # enforce the reservation here too. Admin/form paths also
            # keep calling full_clean() so the user gets a nice error.
            models.CheckConstraint(
                condition=~models.Q(slug__in=sorted(RESERVED_TAG_SLUGS)),
                name="stationtag_slug_not_reserved",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.slug in RESERVED_TAG_SLUGS:
            raise ValidationError({"slug": _("This slug is reserved by the rollout system.")})


class ModuleType(models.Model):
    """Types of hardware modules (e.g., FM Transceiver, Power Board)."""

    class FlashMethod(models.TextChoices):
        USB_DFU = "usb_dfu", _("USB DFU")
        UART = "uart", _("UART")
        SPI = "spi", _("SPI")
        OTHER = "other", _("Other")

    name = models.CharField(_("name"), max_length=100, unique=True)
    slug = models.SlugField(_("slug"), unique=True)
    description = models.TextField(_("description"), blank=True)
    firmware_flash_method = models.CharField(
        _("firmware flash method"),
        max_length=10,
        choices=FlashMethod.choices,
        default=FlashMethod.OTHER,
    )

    class Meta:
        verbose_name = _("module type")
        verbose_name_plural = _("module types")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Station(models.Model):
    """A remote amateur radio station (the main entity)."""

    class Status(models.TextChoices):
        ONLINE = "online", _("Online")
        OFFLINE = "offline", _("Offline")
        UPDATING = "updating", _("Updating")
        ERROR = "error", _("Error")

    name = models.CharField(_("name"), max_length=100)
    callsign = models.CharField(_("callsign"), max_length=20, blank=True)
    description = models.TextField(_("description"), blank=True)

    # Location
    location_name = models.CharField(_("location name"), max_length=255, blank=True)
    latitude = models.DecimalField(
        _("latitude"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        _("longitude"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    altitude = models.IntegerField(
        _("altitude"), null=True, blank=True, help_text=_("Meters above sea level")
    )

    # Hardware
    hardware_revision = models.CharField(_("hardware revision"), max_length=50, blank=True)
    region = models.ForeignKey(
        Region,
        verbose_name=_("region"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stations",
    )
    tags = models.ManyToManyField(
        StationTag,
        verbose_name=_("tags"),
        blank=True,
        related_name="stations",
    )
    installed_modules = models.ManyToManyField(
        ModuleType,
        verbose_name=_("installed modules"),
        blank=True,
        related_name="stations",
    )
    notes = models.TextField(_("notes"), blank=True)

    # Agent-reported state
    current_os_version = models.CharField(_("current OS version"), max_length=100, blank=True)
    current_agent_version = models.CharField(_("current agent version"), max_length=50, blank=True)
    last_ip_address = models.GenericIPAddressField(_("last IP address"), null=True, blank=True)
    last_seen = models.DateTimeField(_("last seen"), null=True, blank=True)
    status = models.CharField(
        _("status"),
        max_length=10,
        choices=Status.choices,
        default=Status.OFFLINE,
    )
    current_image_release = models.ForeignKey(
        "images.ImageRelease",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stations_provisioned_with",
        verbose_name=_("provisioned with image"),
        help_text=_(
            "The image release last used to provision this station. "
            "Set when a provisioning bundle is downloaded."
        ),
    )

    # Timestamps
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("station")
        verbose_name_plural = _("stations")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["status", "last_seen"]),
        ]

    def __str__(self):
        if self.callsign:
            return f"{self.name} ({self.callsign})"
        return self.name

    @property
    def is_online(self):
        """Return True if the station was seen within the last 2 minutes."""
        if self.last_seen is None:
            return False
        return (timezone.now() - self.last_seen).total_seconds() < 120

    def update_from_heartbeat(self, data):
        """Update station fields from a heartbeat payload dict.

        Expected keys (all optional):
            os_version, agent_version, ip_address, status
        """
        if "os_version" in data:
            self.current_os_version = data["os_version"]
        if "agent_version" in data:
            self.current_agent_version = data["agent_version"]
        if "ip_address" in data:
            self.last_ip_address = data["ip_address"]
        if "status" in data and data["status"] in self.Status.values:
            self.status = data["status"]
        self.last_seen = timezone.now()
        self.save(
            update_fields=[
                "current_os_version",
                "current_agent_version",
                "last_ip_address",
                "last_seen",
                "status",
                "updated_at",
            ]
        )


class StationPhoto(models.Model):
    """Photos of a station."""

    station = models.ForeignKey(
        Station,
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="photos",
    )
    image = models.ImageField(_("image"), upload_to="stations/photos/")
    caption = models.CharField(_("caption"), max_length=200, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("uploaded by"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="station_photos",
    )
    uploaded_at = models.DateTimeField(_("uploaded at"), auto_now_add=True)

    class Meta:
        verbose_name = _("station photo")
        verbose_name_plural = _("station photos")
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.station.name} - {self.caption or self.pk}"


class StationLogEntry(models.Model):
    """Logbook for station events."""

    class EntryType(models.TextChoices):
        NOTE = "note", _("Note")
        MAINTENANCE = "maintenance", _("Maintenance")
        INCIDENT = "incident", _("Incident")
        UPDATE = "update", _("Update")
        DEPLOYMENT = "deployment", _("Deployment")

    station = models.ForeignKey(
        Station,
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="log_entries",
    )
    entry_type = models.CharField(
        _("entry type"),
        max_length=12,
        choices=EntryType.choices,
        default=EntryType.NOTE,
    )
    title = models.CharField(_("title"), max_length=200)
    message = models.TextField(_("message"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="station_log_entries",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("station log entry")
        verbose_name_plural = _("station log entries")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.station.name} - {self.title}"


class StationAuditLog(models.Model):
    """Automatic audit trail for all station changes.

    Records every modification to a station: field changes, status
    transitions, heartbeat events, token events, and admin actions.
    """

    class EventType(models.TextChoices):
        CREATED = "created", _("Created")
        UPDATED = "updated", _("Updated")
        DELETED = "deleted", _("Deleted")
        STATUS_CHANGE = "status_change", _("Status Change")
        HEARTBEAT = "heartbeat", _("Heartbeat")
        TOKEN_GENERATED = "token_generated", _("Token Generated")
        TOKEN_REVOKED = "token_revoked", _("Token Revoked")
        FIRMWARE_UPDATE = "firmware_update", _("Firmware Update")
        PROVISIONING_REQUESTED = "provisioning_requested", _("Provisioning Requested")
        PROVISIONING_READY = "provisioning_ready", _("Provisioning Ready")
        PROVISIONING_DOWNLOADED = "provisioning_downloaded", _("Provisioning Downloaded")
        PROVISIONING_FAILED = "provisioning_failed", _("Provisioning Failed")
        PROVISIONING_EXPIRED = "provisioning_expired", _("Provisioning Expired")

    station = models.ForeignKey(
        Station,
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="audit_logs",
    )
    event_type = models.CharField(
        _("event type"),
        max_length=32,
        choices=EventType.choices,
    )
    message = models.TextField(_("message"))
    changes = models.JSONField(
        _("changes"),
        default=dict,
        blank=True,
        help_text=_("JSON dict of changed fields: {field: {old, new}}"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="station_audit_logs",
    )
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("station audit log")
        verbose_name_plural = _("station audit logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["station", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.station.name} - {self.get_event_type_display()} - {self.created_at}"

    @classmethod
    def log(
        cls,
        station=None,
        event_type=None,
        message="",
        changes=None,
        user=None,
        ip_address=None,
        station_id=None,
    ):
        """Convenience method to create an audit log entry.

        Pass either `station` (an instance) or `station_id` (a pk) — the
        station_id form skips the instance fetch for callers that already
        have the pk (e.g. streaming views that captured it before the
        DB session closed).
        """
        if station is None and station_id is None:
            raise ValueError("station or station_id is required")
        if station is not None and station_id is not None:
            raise ValueError("pass either station or station_id, not both")
        if not event_type:
            raise ValueError("event_type is required")
        kwargs = {
            "event_type": event_type,
            "message": message,
            "changes": changes or {},
            "user": user,
            "ip_address": ip_address,
        }
        if station is not None:
            kwargs["station"] = station
        else:
            kwargs["station_id"] = station_id
        return cls.objects.create(**kwargs)


class StationInventory(models.Model):
    """Hardware inventory data reported by the station agent.

    The ``data`` JSONField stores a flexible inventory dict including
    CPU, RAM, disk, network, and OS information.
    """

    station = models.OneToOneField(
        Station,
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="inventory",
    )
    data = models.JSONField(
        _("data"),
        default=dict,
        help_text=_("Hardware inventory data from agent"),
    )
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("station inventory")
        verbose_name_plural = _("station inventories")

    def __str__(self):
        return f"{self.station.name} - inventory"


class _ApplicantForbiddenMixin:
    """Mixin: refuses save() if self.user is a Vereins-Bewerber (APPLICANT).

    Enforces the spec invariant that topology assignments require at
    least Vereins-Mitglied level. Mixed into StationAssignment and
    RegionAssignment so the rule lives in one place.

    save() calls self.clean() (NOT self.full_clean()) deliberately:
    full_clean() runs validate_unique(), which would pre-empt the
    DB-level UniqueConstraint violations as ValidationError. Task 4's
    tests (tests/test_topology_models.py::TestStationAssignment::
    test_uniq_user_per_station, test_uniq_admin_per_station, and
    TestRegionAssignment::test_uniq_user_role_per_region) explicitly
    assert IntegrityError from those constraints. The Applicant rule
    is a cross-table business rule that DB CHECK can't express, so
    Python-side validation via clean() is the correct layer.
    """

    def clean(self):
        super().clean()
        if self.user.membership_level == User.MembershipLevel.APPLICANT:
            raise ValidationError(
                {
                    "user": _(
                        "Vereins-Bewerber können keine Topology-Rolle haben. "
                        "Den User erst zu Vereins-Mitglied promoten."
                    ),
                }
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class StationAssignment(_ApplicantForbiddenMixin, models.Model):
    """Per-user, per-station role assignment.

    Two roles: ADMIN (max 1 per station, the local "owner") and
    MAINTAINER (N per station, co-helpers). Applicant users cannot
    hold an assignment — enforced via clean()/save() in Task 5.
    """

    class Role(models.TextChoices):
        ADMIN = "admin", _("Station-Admin")
        MAINTAINER = "maintainer", _("Station-Maintainer")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="station_assignments",
        verbose_name=_("user"),
    )
    station = models.ForeignKey(
        Station,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("station"),
    )
    role = models.CharField(_("role"), max_length=12, choices=Role.choices)
    assigned_at = models.DateTimeField(_("assigned at"), auto_now_add=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_station_assignments",
        verbose_name=_("assigned by"),
    )

    class Meta:
        verbose_name = _("station assignment")
        verbose_name_plural = _("station assignments")
        ordering = ["station", "role", "user"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "station"],
                name="uniq_user_per_station_assignment",
            ),
            models.UniqueConstraint(
                fields=["station"],
                condition=Q(role="admin"),
                name="uniq_admin_per_station",
            ),
        ]
        indexes = [
            models.Index(fields=["station", "role"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"{self.user} → {self.station} ({self.get_role_display()})"


class RegionAssignment(_ApplicantForbiddenMixin, models.Model):
    """Per-user, per-region role assignment. One role only: MANAGER."""

    class Role(models.TextChoices):
        MANAGER = "manager", _("Region-Manager")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="region_assignments",
        verbose_name=_("user"),
    )
    region = models.ForeignKey(
        Region,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("region"),
    )
    role = models.CharField(_("role"), max_length=10, choices=Role.choices)
    assigned_at = models.DateTimeField(_("assigned at"), auto_now_add=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_region_assignments",
        verbose_name=_("assigned by"),
    )

    class Meta:
        verbose_name = _("region assignment")
        verbose_name_plural = _("region assignments")
        ordering = ["region", "user"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "region", "role"],
                name="uniq_user_role_per_region",
            ),
        ]
        indexes = [
            models.Index(fields=["region", "role"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"{self.user} → {self.region} ({self.get_role_display()})"
