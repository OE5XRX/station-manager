import pytest
from django.utils import timezone

from apps.module_firmware.ingest import ingest_module
from apps.module_firmware.models import Module, ModuleType
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(**kw):
    base = {"type": "fm", "model": "SA818", "version": "1.0.0"}
    base.update(kw)
    return base


@pytest.mark.django_db
def test_no_uid_returns_none_and_creates_no_module(fm, station_factory):
    station = station_factory()
    result = ingest_module(station, "slot1", "fm", ident(), now=timezone.now())
    assert result is None
    assert Module.objects.count() == 0


@pytest.mark.django_db
def test_unknown_type_rejected(station_factory):
    station = station_factory()
    result = ingest_module(
        station, "slot1", "power", ident(type="power", uid="X1"), now=timezone.now()
    )
    assert result is None
    assert Module.objects.count() == 0


@pytest.mark.django_db
def test_new_uid_creates_unregistered_module_and_audits(fm, station_factory):
    station = station_factory()
    now = timezone.now()
    m = ingest_module(station, "slot1", "fm", ident(uid="ABC"), now=now)
    assert m.uid == "ABC"
    assert m.registration_status == Module.Registration.UNREGISTERED
    assert m.first_seen == now and m.last_seen == now
    assert m.last_reported_version == "1.0.0"
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.MODULE_DISCOVERED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_known_uid_updates_only_seen_and_version(fm, station_factory):
    station = station_factory()
    first = timezone.now()
    m1 = ingest_module(station, "slot1", "fm", ident(uid="ABC"), now=first)
    later = first + timezone.timedelta(minutes=5)
    m2 = ingest_module(station, "slot1", "fm", ident(uid="ABC", version="1.1.0"), now=later)
    assert m1.pk == m2.pk
    assert m2.first_seen == first and m2.last_seen == later
    assert m2.last_reported_version == "1.1.0"
    assert (
        StationAuditLog.objects.filter(
            module=m2, event_type=StationAuditLog.EventType.MODULE_DISCOVERED
        ).count()
        == 1
    )
    assert Module.objects.count() == 1
