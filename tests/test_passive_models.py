import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.permissions import can
from apps.passive.models import (
    AprsSettings,
    HandoverProfile,
    PassiveServiceConfig,
    PassiveServiceRun,
    StationPassiveConfig,
    is_valid_igate_callsign,
    normalise_callsign,
)


def test_config_defaults(db):
    c = PassiveServiceConfig.get("aprs")
    assert (c.enabled, c.idle_minutes) == (False, 10)
    assert c.profile["frequency"] == 144.8 and c.profile["squelch"] == 0
    assert c.profile["bandwidth"] == "25" and c.profile["rx_tone"] == "none"
    assert PassiveServiceConfig.get("aprs").pk == c.pk
    assert HandoverProfile.get().values["frequency"] == 145.5
    assert HandoverProfile.get().values["squelch"] == 1
    s = AprsSettings.get()
    assert (
        s.aprsis_host,
        s.aprsis_port,
        s.max_gate_age_s,
        s.retention_days,
        s.no_rx_alert_hours,
        s.beacon_interval_min,
    ) == ("rotate.aprs2.net", 14580, 30, 30, 6, 30)
    assert s.beacon_comment == "OE5XRX Remote Station RX-iGate"


def test_one_open_run_per_station(db, station):
    PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="slot0", state="running"
    )
    assert PassiveServiceRun.open_for(station.id) is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        PassiveServiceRun.objects.create(
            station=station, service="aprs", slot="slot1", state="starting"
        )
    PassiveServiceRun.objects.filter(station=station).update(ended_at=timezone.now())
    assert PassiveServiceRun.open_for(station.id) is None
    PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="slot0", state="starting"
    )


def test_igate_call_property(db, station):
    c = StationPassiveConfig.objects.create(
        station=station, igate_callsign="OE5XRX", igate_ssid=10
    )
    assert c.igate_call == "OE5XRX-10"
    c.igate_ssid = None
    assert c.igate_call is None
    c.igate_ssid, c.igate_callsign = 10, ""
    assert c.igate_call is None


def test_igate_call_unique_and_normalised(db, station_factory):
    assert normalise_callsign(" oe5xrx ") == "OE5XRX"
    a, b = station_factory(), station_factory()
    StationPassiveConfig.objects.create(station=a, igate_callsign="OE5XRX", igate_ssid=10)
    with pytest.raises(IntegrityError), transaction.atomic():
        StationPassiveConfig.objects.create(station=b, igate_callsign="OE5XRX", igate_ssid=10)
    # another SSID and blank calls (unset) never collide
    StationPassiveConfig.objects.create(station=b, igate_callsign="OE5XRX", igate_ssid=11)


@pytest.mark.parametrize(
    "call,ok",
    [("OE5XRX", True), ("OE1ABC", True), ("DL1AB", True), ("W1AW", True), ("oe5xrx", False),
     ("OE5XRX-10", False), ("OE5", False), ("1234", False), ("", False)],
)  # fmt: skip
def test_igate_call_format(call, ok):
    assert is_valid_igate_callsign(call) is ok


def test_permission_area_passive_services(applicant_user, staff_user, admin_user):
    assert can(applicant_user, "passive_services", "view")
    assert not can(staff_user, "passive_services", "manage")
    assert can(admin_user, "passive_services", "manage")
