import pytest
from django.utils import timezone

from apps.module_firmware.ingest import ingest_module
from apps.module_firmware.models import Module, ModuleType
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(uid):
    return {"type": "fm", "version": "1.0.0", "uid": uid}


@pytest.mark.django_db
def test_active_assignment_sets_deployed(fm, station_factory):
    m = ingest_module(station_factory(), "slot1", "fm", ident("A"), now=timezone.now())
    m.refresh_from_db()
    assert m.lifecycle_status == Module.Lifecycle.DEPLOYED
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_sticky_states_not_overridden(fm, station_factory):
    station = station_factory()
    m = ingest_module(station, "slot1", "fm", ident("A"), now=timezone.now())
    m.lifecycle_status = Module.Lifecycle.DEFECT
    m.save(update_fields=["lifecycle_status"])
    ingest_module(station, "slot1", "fm", ident("A"), now=timezone.now())
    m.refresh_from_db()
    assert m.lifecycle_status == Module.Lifecycle.DEFECT
