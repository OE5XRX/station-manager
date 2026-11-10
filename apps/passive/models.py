import re

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

IGATE_CALL_RE = r"^(?=.{3,6}$)[A-Z0-9]{1,3}[0-9][A-Z]{1,4}$"
_IGATE_CALL = re.compile(IGATE_CALL_RE)


def normalise_callsign(s: str) -> str:
    return s.strip().upper()


def is_valid_igate_callsign(s: str) -> bool:
    return bool(_IGATE_CALL.match(s))


def default_aprs_profile():
    return {
        "frequency": 144.8,
        "squelch": 0,
        "rx_tone": "none",
        "tx_tone": "none",
        "bandwidth": "25",
    }


def default_handover_profile():
    return {"frequency": 145.5, "squelch": 1, "rx_tone": "none", "tx_tone": "none"}


class PassiveServiceConfig(models.Model):
    class Service(models.TextChoices):
        APRS = "aprs", "APRS"

    service = models.CharField(_("service"), max_length=16, unique=True, choices=Service.choices)
    enabled = models.BooleanField(_("enabled"), default=False)
    idle_minutes = models.PositiveIntegerField(_("idle minutes"), default=10)
    profile = models.JSONField(_("radio profile"), default=default_aprs_profile)

    class Meta:
        verbose_name = _("passive service")
        verbose_name_plural = _("passive services")

    def __str__(self):
        return self.service

    @classmethod
    def get(cls, service: str) -> "PassiveServiceConfig":
        obj, _created = cls.objects.get_or_create(service=service)
        return obj


class _Singleton(models.Model):
    class Meta:
        abstract = True

    @classmethod
    def get(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class HandoverProfile(_Singleton):
    """Radio profile applied when a passive run ends (so the radio is not left on APRS)."""

    values = models.JSONField(_("values"), default=default_handover_profile)

    class Meta:
        verbose_name = _("hand-over profile")
        verbose_name_plural = _("hand-over profile")


class AprsSettings(_Singleton):
    aprsis_host = models.CharField(_("APRS-IS host"), max_length=255, default="rotate.aprs2.net")
    aprsis_port = models.PositiveIntegerField(_("APRS-IS port"), default=14580)
    aprsis_filter = models.CharField(_("APRS-IS filter"), max_length=255, blank=True)
    max_gate_age_s = models.PositiveIntegerField(_("max gate age (s)"), default=30)
    retention_days = models.PositiveIntegerField(_("raw packet retention (days)"), default=30)
    no_rx_alert_hours = models.PositiveIntegerField(_("no-RX alert after (hours)"), default=6)
    beacon_interval_min = models.PositiveIntegerField(_("beacon interval (min)"), default=30)
    beacon_comment = models.CharField(
        _("beacon comment"), max_length=64, default="OE5XRX Remote Station RX-iGate"
    )

    class Meta:
        verbose_name = _("APRS settings")
        verbose_name_plural = _("APRS settings")


class StationPassiveConfig(models.Model):
    station = models.OneToOneField(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="passive_config",
    )
    enabled = models.BooleanField(_("enabled"), default=False)
    igate_callsign = models.CharField(_("iGate callsign"), max_length=6, blank=True)
    igate_ssid = models.PositiveSmallIntegerField(_("iGate SSID"), null=True, blank=True)

    class Meta:
        verbose_name = _("station passive config")
        verbose_name_plural = _("station passive configs")
        constraints = [
            models.UniqueConstraint(
                fields=["igate_callsign", "igate_ssid"],
                condition=~Q(igate_callsign=""),
                name="uniq_passive_igate_call",
            )
        ]

    def __str__(self):
        return f"{self.station_id}: {self.igate_call or '-'}"

    @property
    def igate_call(self) -> str | None:
        if not self.igate_callsign or self.igate_ssid is None:
            return None
        return f"{self.igate_callsign}-{self.igate_ssid}"


class PassiveServiceRun(models.Model):
    class State(models.TextChoices):
        STARTING = "starting", _("Starting")
        RUNNING = "running", _("Running")
        STOPPING = "stopping", _("Stopping")
        AGENT_OFFLINE = "agent_offline", _("Agent offline")
        STOPPED = "stopped", _("Stopped")
        ERROR = "error", _("Error")

    RUNNING_STATES = {"running"}

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="passive_runs",
    )
    service = models.CharField(_("service"), max_length=16)
    slot = models.CharField(_("slot"), max_length=8)
    module_id = models.CharField(_("module"), max_length=128, blank=True)
    state = models.CharField(_("state"), max_length=16, choices=State.choices)
    started_at = models.DateTimeField(_("started at"), auto_now_add=True)
    ended_at = models.DateTimeField(_("ended at"), null=True, blank=True)
    end_reason = models.CharField(_("end reason"), max_length=32, blank=True)
    last_error = models.CharField(_("last error"), max_length=256, blank=True)
    last_packet_at = models.DateTimeField(_("last packet at"), null=True, blank=True)
    packet_count = models.PositiveBigIntegerField(_("packet count"), default=0)

    class Meta:
        verbose_name = _("passive service run")
        verbose_name_plural = _("passive service runs")
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["station"],
                condition=Q(ended_at__isnull=True),
                name="uniq_open_passive_run",
            )
        ]

    def __str__(self):
        return f"{self.station_id}/{self.service} {self.state}"

    @classmethod
    def open_for(cls, station_id) -> "PassiveServiceRun | None":
        return cls.objects.filter(station_id=station_id, ended_at__isnull=True).first()


class AprsPacket(models.Model):
    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="aprs_packets",
    )
    run = models.ForeignKey(
        PassiveServiceRun,
        verbose_name=_("run"),
        null=True,
        on_delete=models.SET_NULL,
        related_name="packets",
    )
    epoch = models.CharField(_("epoch"), max_length=32)
    seq = models.PositiveBigIntegerField(_("sequence"))
    heard_at = models.DateTimeField(_("heard at"))
    received_at = models.DateTimeField(_("received at"))
    raw_tnc2 = models.TextField(_("raw TNC2"))
    source = models.CharField(_("source"), max_length=9)
    destination = models.CharField(_("destination"), max_length=9)
    path = models.JSONField(_("path"), default=list)
    digipeated = models.BooleanField(_("digipeated"), default=False)
    packet_type = models.CharField(_("type"), max_length=16)
    latitude = models.DecimalField(_("latitude"), max_digits=9, decimal_places=6, null=True)
    longitude = models.DecimalField(_("longitude"), max_digits=9, decimal_places=6, null=True)
    symbol = models.CharField(_("symbol"), max_length=2, blank=True)
    comment = models.CharField(_("comment"), max_length=256, blank=True)
    gated_at = models.DateTimeField(_("gated at"), null=True, blank=True)
    gate_skip = models.CharField(_("gate skip reason"), max_length=24, blank=True)

    class Meta:
        verbose_name = _("APRS packet")
        verbose_name_plural = _("APRS packets")
        constraints = [
            models.UniqueConstraint(fields=["station", "epoch", "seq"], name="uniq_aprs_pkt_seq")
        ]
        indexes = [
            models.Index(fields=["heard_at"], name="aprs_pkt_heard_idx"),
            models.Index(fields=["station", "heard_at"], name="aprs_pkt_st_heard_idx"),
            models.Index(fields=["source", "heard_at"], name="aprs_pkt_src_heard_idx"),
        ]

    def __str__(self):
        return f"{self.source}>{self.destination} @{self.heard_at:%H:%M:%S}"


class CoverageCell(models.Model):
    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="coverage_cells",
    )
    cell_lat = models.IntegerField(_("cell latitude"))
    cell_lon = models.IntegerField(_("cell longitude"))
    count = models.PositiveBigIntegerField(_("count"), default=0)
    first_heard = models.DateTimeField(_("first heard"))
    last_heard = models.DateTimeField(_("last heard"))
    max_distance_km = models.FloatField(_("max distance (km)"), default=0)

    class Meta:
        verbose_name = _("coverage cell")
        verbose_name_plural = _("coverage cells")
        constraints = [
            models.UniqueConstraint(
                fields=["station", "cell_lat", "cell_lon"], name="uniq_coverage_cell"
            )
        ]
