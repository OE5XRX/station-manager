"""Stale-module cleanup (#165) vs. firmware flashing (Teilbereich D).

A module the agent is flashing (inventory entry ``status == "updating"`` for a paused
slot, or a ``firmware_updating`` row from a started-but-unfinished flash) must never be
pruned by the TTL, never be treated as moved/re-plugged, and the flash itself must not
run its TTL down.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.control import registry
from apps.control.models import PersistedCapability, StationModule
from apps.module_firmware.models import ModuleType
from apps.stations.models import Station, StationAuditLog

TTL = timedelta(hours=48)


@pytest.fixture
def fm_type(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture
def station(db):
    return Station.objects.create(name="prune-vs-updating")


DESC = [
    {"name": "frequency", "kind": "setting", "type": "float"},
    {"name": "volume", "kind": "setting", "type": "int", "min": 1, "max": 8},
]


def _fm(uid="UID-A", status=None, module="fm", state=None):
    mod = {
        "module": module,
        "identity": {"type": "fm_transceiver", "model": "SA818", "version": "1", "uid": uid},
        "capabilities": DESC,
        "state": {} if state is None else state,
    }
    if status is not None:
        mod["status"] = status
    return mod


def _inv(**slots):
    return [{"slot": s, "modules": mods} for s, mods in slots.items()]


def _backdate(station, slot, age):
    when = timezone.now() - age
    StationModule.objects.filter(station=station, slot=slot).update(
        last_seen=when, updated_at=when
    )


def _swapped_logs(station):
    return StationAuditLog.objects.filter(
        station=station,
        event_type=StationAuditLog.EventType.MODULE_SWAPPED,
        message__startswith="Control panel:",  # registry carry-over, not ingest's own
    )


# -- (a) TTL ------------------------------------------------------------------------


@pytest.mark.django_db
def test_updating_entry_older_than_ttl_is_not_pruned_and_ttl_refreshed(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm()], slot2=[_fm(uid="UID-B")]))
    _backdate(station, "slot1", TTL + timedelta(hours=1))

    registry.apply_inventory(
        station, _inv(slot1=[_fm(status="updating")], slot2=[_fm(uid="UID-B")])
    )

    sm = StationModule.objects.get(station=station, slot="slot1")
    assert sm.firmware_updating is True
    assert sm.online is True
    assert timezone.now() - sm.last_seen < timedelta(minutes=1)


@pytest.mark.django_db
def test_offline_flashing_row_is_not_pruned(fm_type, station):
    """Started event on a soft-offline row (fast cycle, no ``updating`` inventory yet):
    the row is offline + updating and must survive a snapshot past the TTL."""
    registry.apply_inventory(station, _inv(slot1=[_fm()], slot2=[_fm(uid="UID-B")]))
    registry.apply_inventory(station, _inv(slot2=[_fm(uid="UID-B")]))  # slot1 drops out
    _backdate(station, "slot1", TTL + timedelta(hours=1))
    registry.set_firmware_updating(station, "slot1", "fm", True)

    registry.apply_inventory(station, _inv(slot2=[_fm(uid="UID-B")]))

    sm = StationModule.objects.get(station=station, slot="slot1")
    assert sm.firmware_updating is True
    assert sm.online is False


@pytest.mark.django_db
def test_finished_flash_restarts_ttl(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm()], slot2=[_fm(uid="UID-B")]))
    _backdate(station, "slot1", TTL + timedelta(hours=1))
    registry.set_firmware_updating(station, "slot1", "fm", True)
    registry.set_firmware_updating(station, "slot1", "fm", False, result="success")

    # Module still re-enumerating: not in this snapshot — must not be pruned at once.
    registry.apply_inventory(station, _inv(slot2=[_fm(uid="UID-B")]))

    sm = StationModule.objects.get(station=station, slot="slot1")
    assert sm.online is False
    assert sm.firmware_updating is False
    assert timezone.now() - sm.last_seen < timedelta(minutes=1)


# -- (b) re-plug carry-over ---------------------------------------------------------


@pytest.mark.django_db
def test_updating_entry_is_not_a_move_and_keeps_settings(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm(state={"volume": 3})]))
    PersistedCapability.objects.create(
        station=station, slot="slot1", module_id="fm", capability="volume", value=6
    )

    registry.apply_inventory(station, _inv(slot1=[_fm(status="updating")]))

    rows = list(StationModule.objects.filter(station=station))
    assert [(r.slot, r.module_id) for r in rows] == [("slot1", "fm")]
    assert rows[0].last_state == {"volume": 3}
    assert list(
        PersistedCapability.objects.filter(station=station).values_list(
            "slot", "capability", "value"
        )
    ) == [("slot1", "volume", 6)]
    assert not _swapped_logs(station).exists()


@pytest.mark.django_db
def test_flashing_module_reported_elsewhere_is_not_moved(fm_type, station):
    """Same uid shows up normally in another slot while its slot is still paused for
    the flash (e.g. a debounced last-known entry): neither the flashing row nor its
    calibration may be deleted/moved."""
    registry.apply_inventory(station, _inv(slot1=[_fm(state={"volume": 3})]))
    PersistedCapability.objects.create(
        station=station, slot="slot1", module_id="fm", capability="volume", value=6
    )

    registry.apply_inventory(station, _inv(slot1=[_fm(status="updating")], slot2=[_fm()]))

    flashing = StationModule.objects.get(station=station, slot="slot1")
    assert flashing.firmware_updating is True
    assert list(
        PersistedCapability.objects.filter(station=station).values_list(
            "slot", "capability", "value"
        )
    ) == [("slot1", "volume", 6)]
    assert not _swapped_logs(station).exists()


# -- (c) normal #165 behaviour unchanged --------------------------------------------


@pytest.mark.django_db
def test_normal_replug_and_prune_still_work(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm()], slot3=[_fm(uid="UID-C")]))
    registry.apply_inventory(station, _inv(slot1=[_fm()]))  # slot3 drops out
    _backdate(station, "slot3", TTL + timedelta(hours=1))

    registry.apply_inventory(station, _inv(slot2=[_fm()]))  # UID-A re-plugged

    keys = sorted(StationModule.objects.filter(station=station).values_list("slot", "module_id"))
    assert keys == [("slot2", "fm")]
    assert _swapped_logs(station).count() == 1
