from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ModuleType(models.Model):
    """Registry of flashable module types (only firmware-bearing types)."""

    key = models.SlugField(_("key"), unique=True, help_text=_("z. B. fm, power, device-tester"))
    display_name = models.CharField(_("display name"), max_length=128)
    hw_repo = models.CharField(_("hardware repo"), max_length=200, blank=True)
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
        _("registration"), max_length=16,
        choices=Registration.choices, default=Registration.UNREGISTERED,
    )
    last_reported_version = models.CharField(_("last reported version"), max_length=64, blank=True)
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
            models.Index(fields=["uid"]),
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
        "stations.Station", verbose_name=_("station"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name="module_assignments",
    )
    slot = models.CharField(_("slot"), max_length=64)
    from_ts = models.DateTimeField(_("from"), default=timezone.now)
    to_ts = models.DateTimeField(_("to"), null=True, blank=True)
    reason = models.CharField(_("reason"), max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("created by"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = _("module assignment")
        verbose_name_plural = _("module assignments")
        ordering = ["-from_ts"]
        constraints = [
            models.UniqueConstraint(
                fields=["module"], condition=models.Q(to_ts__isnull=True),
                name="uniq_open_assignment_per_module",
            ),
            models.UniqueConstraint(
                fields=["station", "slot"], condition=models.Q(to_ts__isnull=True),
                name="uniq_open_assignment_per_station_slot",
            ),
        ]

    def __str__(self):
        return f"{self.module.uid} @ {self.station_id}/{self.slot}"
