import pytest
from django.utils import timezone

from apps.module_firmware.ingest import ingest_module
from apps.module_firmware.models import ModuleAssignmentHistory, ModuleType
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(uid, v="1.0.0"):
    return {"type": "fm", "model": "SA818", "version": v, "uid": uid}


@pytest.mark.django_db
def test_first_seen_opens_assignment(fm, station_factory):
    station = station_factory()
    m = ingest_module(station, "slot1", "fm", ident("A"), now=timezone.now())
    open_rows = ModuleAssignmentHistory.objects.filter(module=m, to_ts__isnull=True)
    assert open_rows.count() == 1
    assert open_rows.first().station == station and open_rows.first().slot == "slot1"


@pytest.mark.django_db
def test_idempotent_same_slot_no_new_rows(fm, station_factory):
    station = station_factory()
    now = timezone.now()
    m = ingest_module(station, "slot1", "fm", ident("A"), now=now)
    ingest_module(
        station, "slot1", "fm", ident("A", v="1.1.0"), now=now + timezone.timedelta(minutes=1)
    )
    assert ModuleAssignmentHistory.objects.filter(module=m).count() == 1
    assert (
        StationAuditLog.objects.filter(event_type=StationAuditLog.EventType.MODULE_SWAPPED).count()
        == 0
    )


@pytest.mark.django_db
def test_new_uid_in_slot_swaps(fm, station_factory):
    station = station_factory()
    t0 = timezone.now()
    a = ingest_module(station, "slot1", "fm", ident("A"), now=t0)
    t1 = t0 + timezone.timedelta(hours=1)
    b = ingest_module(station, "slot1", "fm", ident("B"), now=t1)
    a_row = ModuleAssignmentHistory.objects.get(module=a)
    assert a_row.to_ts == t1
    assert ModuleAssignmentHistory.objects.filter(module=b, to_ts__isnull=True).count() == 1
    assert (
        StationAuditLog.objects.filter(
            module=b, event_type=StationAuditLog.EventType.MODULE_SWAPPED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_module_moved_to_new_slot_closes_old(fm, station_factory):
    station = station_factory()
    t0 = timezone.now()
    m = ingest_module(station, "slot1", "fm", ident("A"), now=t0)
    t1 = t0 + timezone.timedelta(hours=1)
    ingest_module(station, "slot2", "fm", ident("A"), now=t1)
    rows = ModuleAssignmentHistory.objects.filter(module=m).order_by("from_ts")
    assert rows.count() == 2
    assert rows[0].slot == "slot1" and rows[0].to_ts == t1
    assert rows[1].slot == "slot2" and rows[1].to_ts is None
