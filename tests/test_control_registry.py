import pytest
from django.db import IntegrityError

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
