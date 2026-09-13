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
def test_unknown_type_writes_no_audit_row(station_factory):
    """Rejected unknown-type entries must not append an audit row per heartbeat."""
    station = station_factory()
    for _ in range(3):
        ingest_module(station, "slot1", "power", ident(type="power", uid="X1"), now=timezone.now())
    assert StationAuditLog.objects.count() == 0


@pytest.mark.django_db
def test_type_mismatch_on_existing_uid_is_rejected(fm, station_factory):
    """A report claiming a different registered type for an existing UID is
    rejected (UID is a stable identity) and audited, without reassigning."""
    ModuleType.objects.create(key="power", display_name="Power")
    station = station_factory()
    m = ingest_module(station, "slot1", "fm", ident(uid="DUP"), now=timezone.now())
    assert m.module_type.key == "fm"

    # Repeat the mismatch several times: audit must be written only once.
    for _ in range(3):
        result = ingest_module(
            station, "slot2", "power", ident(type="power", uid="DUP"), now=timezone.now()
        )
        assert result is None
    m.refresh_from_db()
    assert m.module_type.key == "fm"  # unchanged
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.UPDATED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_version_change_is_audited(fm, station_factory):
    """A firmware version change on a known module records an audited transition;
    a repeated same-version report does not."""
    station = station_factory()
    t0 = timezone.now()
    ingest_module(station, "slot1", "fm", ident(uid="V"), now=t0)  # 1.0.0, first seen
    ingest_module(station, "slot1", "fm", ident(uid="V", version="1.0.0"), now=t0)  # no change
    m = Module.objects.get(uid="V")
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.FIRMWARE_UPDATE
        ).count()
        == 0
    )

    ingest_module(station, "slot1", "fm", ident(uid="V", version="2.0.0"), now=t0)
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.FIRMWARE_UPDATE
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
