import pytest

from apps.control.models import StationModule
from apps.module_firmware.models import Module, ModuleType


@pytest.mark.django_db
def test_stationmodule_links_to_module(station_factory):
    station = station_factory()
    t = ModuleType.objects.create(key="fm", display_name="FM")
    mod = Module.objects.create(uid="U1", module_type=t)
    sm = StationModule.objects.create(
        station=station, slot="slot1", module_id="fm", tracked_module=mod
    )
    assert sm.tracked_module == mod
    assert mod.station_modules.first() == sm
