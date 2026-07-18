import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.stations.models import Station


@pytest.mark.django_db
def test_stationmodule_unique_slot_module():
    from apps.control.models import StationModule

    station = Station.objects.create(name="s1")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0")
    with pytest.raises(IntegrityError):
        StationModule.objects.create(station=station, slot="slot0", module_id="fm0")


@pytest.mark.django_db
def test_stationmodule_defaults():
    from apps.control.models import StationModule

    station = Station.objects.create(name="s2")
    m = StationModule.objects.create(station=station, slot="slot1", module_id="fm0")
    assert m.online is False
    assert m.last_state == {}
    assert m.capability_descriptor == []
    assert m.last_seen is None


def _fm_descriptor():
    return [
        {"name": "frequency", "kind": "setting", "type": "float"},
        {"name": "rssi", "kind": "telemetry", "type": "int"},
    ]


@pytest.mark.django_db
def test_apply_inventory_upserts_and_marks_offline():
    from apps.control import registry
    from apps.control.models import StationModule

    station = Station.objects.create(name="inv1")
    # A previously-known module that will NOT be in the new inventory.
    stale = StationModule.objects.create(
        station=station, slot="slot9", module_id="old0", online=True
    )

    slots = [
        {
            "slot": "slot0",
            "modules": [
                {
                    "module": "fm0",
                    "identity": {"type": "fm", "model": "SA818", "version": "1.2"},
                    "capabilities": _fm_descriptor(),
                    # Include a telemetry cap (rssi) in the snapshot: it must be
                    # filtered out of last_state on persist.
                    "state": {"frequency": 145.5, "rssi": -70},
                }
            ],
        }
    ]
    registry.apply_inventory(station, slots)

    m = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert m.online is True
    assert m.type == "fm" and m.model == "SA818" and m.version == "1.2"
    assert m.capability_descriptor == _fm_descriptor()
    assert m.last_state == {"frequency": 145.5}  # rssi (telemetry) NOT persisted
    assert m.last_seen is not None

    stale.refresh_from_db()
    assert stale.online is False  # soft — still present


@pytest.mark.django_db
def test_apply_state_persists_settings_not_telemetry():
    from apps.control import registry
    from apps.control.models import StationModule

    station = Station.objects.create(name="st1")
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm0",
        capability_descriptor=_fm_descriptor(),
        last_state={"frequency": 145.5},
    )
    registry.apply_state(station, "slot0", "fm0", {"frequency": 146.0, "rssi": -70})

    m = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert m.last_state == {"frequency": 146.0}  # rssi (telemetry) NOT persisted


@pytest.mark.django_db
def test_apply_state_unknown_module_is_noop():
    from apps.control import registry

    station = Station.objects.create(name="st2")
    registry.apply_state(station, "slotX", "nope", {"frequency": 1.0})  # must not raise


@pytest.mark.django_db
def test_mark_station_offline():
    from apps.control import registry
    from apps.control.models import StationModule

    station = Station.objects.create(name="off1")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)
    registry.mark_station_offline(station)
    assert StationModule.objects.filter(station=station, online=True).count() == 0
