"""``ineligible_reason``: one row per reason, in the documented order."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.control.models import ControlLock
from apps.passive.eligibility import REASONS, ineligible_reason
from apps.passive.models import PassiveServiceConfig, StationPassiveConfig
from apps.stations.models import Station
from tests.test_passive_profiles import make_descriptor, make_module

FEATURES = {"decoders"}


@pytest.fixture
def ready(station):
    """A station that is eligible; each case breaks exactly one thing."""
    cfg = PassiveServiceConfig.objects.create(service="aprs", enabled=True, idle_minutes=10)
    StationPassiveConfig.objects.create(
        station=station, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    make_module(station)
    return cfg


def reason(station, cfg, **kw):
    station = Station.objects.get(pk=station.pk)  # fresh: no cached passive_config
    args = {"now": timezone.now(), "agent_connected": True, "agent_features": FEATURES}
    args.update(kw)
    return ineligible_reason(station, cfg, **args)


def test_eligible(station, ready):
    assert reason(station, ready) is None


def test_disabled(station, ready):
    ready.enabled = False
    assert reason(station, ready) == "disabled"


def test_not_opted_in_without_row(station, ready):
    StationPassiveConfig.objects.all().delete()
    assert reason(station, ready) == "not_opted_in"


def test_not_opted_in_without_call(station, ready):
    StationPassiveConfig.objects.update(igate_callsign="")
    assert reason(station, ready) == "not_opted_in"


def test_offline(station, ready):
    assert reason(station, ready, agent_connected=False) == "offline"


@pytest.mark.parametrize("features", [set(), {"other"}])
def test_agent_too_old(station, ready, features):
    assert reason(station, ready, agent_features=features) == "agent_too_old"


def test_updating(station, ready):
    Station.objects.filter(pk=station.pk).update(status=Station.Status.UPDATING)
    assert reason(station, ready) == "updating"


def test_draining(station, ready, monkeypatch):
    monkeypatch.setattr("apps.control.lock.is_draining", lambda s: True)
    assert reason(station, ready) == "draining"


def test_lock_held(station, ready, member_user):
    ControlLock.objects.create(station=station, holder=member_user)
    assert reason(station, ready) == "lock_held"


def test_idle_wait_then_eligible(station, ready):
    now = timezone.now()
    ControlLock.objects.create(station=station, released_at=now - timedelta(minutes=3))
    assert reason(station, ready, now=now) == "idle_wait"
    assert reason(station, ready, now=now + timedelta(minutes=8)) is None


def test_never_held_counts_as_idle(station, ready):
    ControlLock.objects.create(station=station, released_at=None)
    assert reason(station, ready) is None


def test_no_module(station, ready):
    station.modules.all().delete()
    assert reason(station, ready) == "no_module"


def test_no_module_for_other_band(station, ready):
    station.modules.all().delete()
    make_module(station, descriptor=make_descriptor(band=("70cm", 430, 440)))
    assert reason(station, ready) == "no_module"


def test_reason_codes_are_documented():
    assert set(REASONS) >= {"disabled", "idle_wait", "no_module", "lock_held"}
