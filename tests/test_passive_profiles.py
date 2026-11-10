"""Passive-service profiles, slot selection and the ``released_at`` idle timer."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.control import lock
from apps.control.models import ControlLock, PersistedCapability, StationModule
from apps.passive.profiles import handover_values, profile_caps, profile_commands
from apps.passive.selection import pick_slot


def make_descriptor(*, band=("2m", 144, 146), extra=()):
    names = ["frequency", "squelch", "volume", "rx_tone", "tx_tone", "bandwidth", "filter_hp"]
    descriptor = [{"name": n, "kind": "setting"} for n in (*names, *extra)]
    descriptor.append(
        {
            "name": "rx_frequency",
            "kind": "setting",
            "ranges": [{"name": band[0], "min": band[1], "max": band[2]}],
        }
    )
    return descriptor


def make_module(station, slot="1", module_id="fm-1", descriptor=None, **kw):
    return StationModule.objects.create(
        station=station,
        slot=slot,
        module_id=module_id,
        capability_descriptor=make_descriptor() if descriptor is None else descriptor,
        online=kw.pop("online", True),
        **kw,
    )


@pytest.fixture
def fm_module(station):
    return make_module(station)


def test_profile_commands_filters_unknown_and_filter_caps(fm_module):
    cmds = profile_commands(
        {"squelch": 0, "frequency": 144.8, "filter_hp": 1, "bogus": 1}, fm_module
    )
    assert cmds == [("frequency", 144.8), ("squelch", 0)]


def test_profile_commands_drops_non_setting_caps(station):
    descriptor = [
        {"name": "frequency", "kind": "telemetry"},
        {"name": "squelch", "kind": "setting"},
    ]
    module = make_module(station, descriptor=descriptor)
    assert profile_commands({"frequency": 144.8, "squelch": 0}, module) == [("squelch", 0)]
    assert profile_caps({"frequency": 144.8, "squelch": 0}, module) == {"squelch"}


def test_profile_commands_follow_profile_order(fm_module):
    values = {"volume": 3, "tx_tone": "none", "bandwidth": "25", "frequency": 144.8}
    assert [n for n, _v in profile_commands(values, fm_module)] == [
        "frequency",
        "bandwidth",
        "tx_tone",
        "volume",
    ]


def test_handover_adds_persisted_volume(station, fm_module):
    PersistedCapability.objects.create(
        station=station, slot="1", module_id="fm-1", capability="volume", value=7
    )
    values = handover_values(station, fm_module, {"frequency": 145.5})
    assert values == {"frequency": 145.5, "volume": 7}


def test_handover_explicit_volume_wins(station, fm_module):
    PersistedCapability.objects.create(
        station=station, slot="1", module_id="fm-1", capability="volume", value=7
    )
    assert handover_values(station, fm_module, {"volume": 2})["volume"] == 2


def test_handover_without_persisted_volume(station, fm_module):
    assert handover_values(station, fm_module, {"squelch": 1}) == {"squelch": 1}


def test_pick_slot_first_matching_by_numeric_slot(station):
    make_module(station, slot="10", module_id="a")
    make_module(station, slot="2", module_id="b")
    assert pick_slot(station, Decimal("144.8")).slot == "2"


def test_pick_slot_none_for_70cm_only(station):
    make_module(station, descriptor=make_descriptor(band=("70cm", 430, 440)))
    assert pick_slot(station, Decimal("144.8")) is None


def test_pick_slot_skips_offline_and_updating(station):
    make_module(station, slot="1", module_id="a", online=False)
    make_module(station, slot="2", module_id="b", firmware_updating=True)
    make_module(station, slot="3", module_id="c")
    assert pick_slot(station, Decimal("144.8")).slot == "3"


def test_released_at_set_by_every_free_path(station, member_user):
    rec = ControlLock.objects.create(station=station)
    assert rec.released_at is None  # never held

    lock.acquire(station, member_user)
    lock.release(station, member_user)
    first = ControlLock.objects.get(pk=rec.pk).released_at
    assert first is not None

    lock.acquire(station, member_user)
    assert lock.force_free(station) is True
    second = ControlLock.objects.get(pk=rec.pk).released_at
    assert second > first

    lock.acquire(station, member_user)
    later = timezone.now() + timedelta(hours=1)
    assert lock.sweep_lock(station, later, idle_seconds=60) is True
    assert ControlLock.objects.get(pk=rec.pk).released_at > second


def test_pick_slot_ignores_non_ascii_digit_slots(station):
    make_module(station, slot="\u00b2", module_id="x")
    assert pick_slot(station, Decimal("144.8")) is None
