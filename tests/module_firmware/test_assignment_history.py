import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.module_firmware.models import Module, ModuleAssignmentHistory, ModuleType


@pytest.fixture
def module(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    return Module.objects.create(uid="U1", module_type=t)


@pytest.mark.django_db
def test_only_one_open_assignment_per_module(module, station_factory):
    station = station_factory()
    ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot1")
    with pytest.raises(IntegrityError):
        ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot2")


@pytest.mark.django_db
def test_closed_assignment_allows_new_open(module, station_factory):
    station = station_factory()
    old = ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot1")
    old.to_ts = timezone.now()
    old.save()
    ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot2")  # ok
