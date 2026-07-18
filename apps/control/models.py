from django.db import models
from django.utils.translation import gettext_lazy as _


class StationModule(models.Model):
    """A module discovered on a station via the agent's ``inventory`` snapshot.

    Descriptor + last settings state are persisted so the UI can render the
    panel even while the station is offline. Telemetry is never stored here.
    """

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="modules",
    )
    slot = models.CharField(_("slot"), max_length=64)
    module_id = models.CharField(_("module id"), max_length=128)

    # Identity (from inventory ``identity``).
    type = models.CharField(_("type"), max_length=128, blank=True)
    model = models.CharField(_("model"), max_length=128, blank=True)
    version = models.CharField(_("version"), max_length=64, blank=True)

    capability_descriptor = models.JSONField(_("capability descriptor"), default=list, blank=True)
    last_state = models.JSONField(_("last state"), default=dict, blank=True)

    online = models.BooleanField(_("online"), default=False)
    last_seen = models.DateTimeField(_("last seen"), null=True, blank=True)

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("station module")
        verbose_name_plural = _("station modules")
        ordering = ["station", "slot", "module_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["station", "slot", "module_id"],
                name="uniq_station_slot_module",
            ),
        ]
        indexes = [
            models.Index(fields=["station", "online"]),
        ]

    def __str__(self):
        return f"{self.station_id}/{self.slot}/{self.module_id}"


class ControlLock(models.Model):
    """Per-(station, scope) TX-lock. USER-owned (shared across the user's tabs).

    ``scope`` is ``"station"`` today; the unique key leaves room to extend to
    per-module or role scopes later without a schema change to the holder logic.
    """

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="control_locks",
    )
    scope = models.CharField(_("scope"), max_length=64, default="station")
    holder = models.ForeignKey(
        "accounts.User",
        verbose_name=_("holder"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="held_control_locks",
    )
    acquired_at = models.DateTimeField(_("acquired at"), null=True, blank=True)
    last_activity = models.DateTimeField(_("last activity"), null=True, blank=True)
    pending_release_at = models.DateTimeField(_("pending release at"), null=True, blank=True)

    class Meta:
        verbose_name = _("control lock")
        verbose_name_plural = _("control locks")
        constraints = [
            models.UniqueConstraint(fields=["station", "scope"], name="uniq_station_scope_lock"),
        ]

    def __str__(self):
        who = self.holder_id or "FREE"
        return f"lock({self.station_id}/{self.scope})={who}"
