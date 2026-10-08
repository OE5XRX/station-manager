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

    # NOTE: named ``tracked_module`` (not ``module``) because ``module_id`` above
    # is an existing CharField; a FK named ``module`` would collide on the
    # implicit ``module_id`` attname/column.
    tracked_module = models.ForeignKey(
        "module_firmware.Module",
        verbose_name=_("module"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="station_modules",
    )

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


class PersistedCapability(models.Model):
    """Server-persisted value of a role-gated calibration capability (spec §4a).

    The FW persists nothing; the server stores the last successfully applied value per
    (station, slot, module, capability) and re-applies it through the agent whenever an
    inventory shows the module disagreeing. Only capabilities whose policy has
    ``persist=True`` are ever written here (see ``apps.control.persistence``).
    """

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="persisted_capabilities",
    )
    # Same type/length as StationModule.slot (inventory slots are keyed as str).
    slot = models.CharField(_("slot"), max_length=64)
    module_id = models.CharField(_("module id"), max_length=128)
    capability = models.CharField(_("capability"), max_length=64)
    value = models.JSONField(_("value"))
    updated_by = models.ForeignKey(
        "accounts.User",
        verbose_name=_("updated by"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("persisted capability")
        verbose_name_plural = _("persisted capabilities")
        ordering = ["station", "slot", "module_id", "capability"]
        constraints = [
            models.UniqueConstraint(
                fields=["station", "slot", "module_id", "capability"],
                name="uniq_persisted_cap",
            ),
        ]

    def __str__(self):
        return f"{self.station_id}/{self.slot}/{self.module_id}.{self.capability}={self.value!r}"


class AgentConnection(models.Model):
    """The CURRENT agent WebSocket per (station, kind) — the stale-disconnect guard.

    An agent reconnect overlaps its predecessor: through the tunnel the server often
    notices the old socket is dead only after the new one connected and sent inventory.
    Each agent consumer claims this row on connect (last writer wins) and runs its
    station-wide teardown on disconnect only if it can still release the row as its own
    (compare-and-delete). Lives in the DB, not process memory, because the old and new
    connection may be served by different ASGI workers. See ``apps.control.agent_presence``.
    """

    class Kind(models.TextChoices):
        CONTROL = "control", _("control")
        AUDIO = "audio", _("audio")
        TERMINAL = "terminal", _("terminal")

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="agent_connections",
    )
    kind = models.CharField(_("kind"), max_length=16, choices=Kind.choices)
    channel_name = models.CharField(_("channel name"), max_length=255)
    connected_at = models.DateTimeField(_("connected at"), auto_now=True)

    class Meta:
        verbose_name = _("agent connection")
        verbose_name_plural = _("agent connections")
        constraints = [
            models.UniqueConstraint(fields=["station", "kind"], name="uniq_agent_connection"),
        ]

    def __str__(self):
        return f"{self.station_id}/{self.kind}={self.channel_name}"
