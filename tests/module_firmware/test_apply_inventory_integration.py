import pytest

from apps.control.models import StationModule
from apps.control.registry import apply_inventory
from apps.module_firmware.models import Module, ModuleAssignmentHistory, ModuleType


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def slots(uid=None, version="1.0.0"):
    identity = {"type": "fm", "model": "SA818", "version": version}
    if uid:
        identity["uid"] = uid
    return [
        {
            "slot": "slot1",
            "control": "/dev/x",
            "modules": [{"module": "fm", "identity": identity, "capabilities": [], "state": {}}],
        }
    ]


@pytest.mark.django_db
def test_apply_inventory_links_module(fm, station_factory):
    station = station_factory()
    apply_inventory(station, slots(uid="ABC"))
    sm = StationModule.objects.get(station=station, slot="slot1", module_id="fm")
    assert sm.tracked_module is not None and sm.tracked_module.uid == "ABC"
    assert Module.objects.count() == 1
    assert (
        ModuleAssignmentHistory.objects.filter(
            module=sm.tracked_module, to_ts__isnull=True
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_apply_inventory_legacy_no_uid_leaves_module_null(fm, station_factory):
    station = station_factory()
    apply_inventory(station, slots(uid=None))
    sm = StationModule.objects.get(station=station, slot="slot1", module_id="fm")
    assert sm.tracked_module is None
    assert Module.objects.count() == 0
