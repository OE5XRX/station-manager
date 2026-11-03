import pytest
from django.utils import timezone

from apps.module_firmware.ingest import ingest_module
from apps.module_firmware.models import Module, ModuleType
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(**kw):
    base = {"type": "fm", "model": "SA818", "version": "1.0.0", "uid": "BK1"}
    base.update(kw)
    return base


def _ingest(station, **kw):
    return ingest_module(station, "slot1", "fm", ident(**kw), now=timezone.now())


@pytest.mark.django_db
def test_ingest_stores_boot_key(fm, station_factory):
    m = _ingest(station_factory(), boot_key="E30466F6B8470C1F")
    assert Module.objects.get(pk=m.pk).reported_boot_key == "e30466f6b8470c1f"


@pytest.mark.django_db
def test_ingest_updates_boot_key_on_existing(fm, station_factory):
    station = station_factory()
    m = _ingest(station, boot_key="e30466f6b8470c1f")
    _ingest(station, boot_key="0123456789abcdef")
    assert Module.objects.get(pk=m.pk).reported_boot_key == "0123456789abcdef"


@pytest.mark.django_db
def test_ingest_clears_boot_key_when_field_absent(fm, station_factory):
    station = station_factory()
    m = _ingest(station, boot_key="e30466f6b8470c1f")
    _ingest(station)
    assert Module.objects.get(pk=m.pk).reported_boot_key == ""


@pytest.mark.django_db
def test_ingest_rejects_malformed_boot_key(fm, station_factory):
    station = station_factory()
    m = _ingest(station, boot_key="xyz")
    assert Module.objects.get(pk=m.pk).reported_boot_key == ""
    baseline = StationAuditLog.objects.filter(module=m).count()
    _ingest(station, boot_key="xyz")
    assert StationAuditLog.objects.filter(module=m).count() == baseline


@pytest.mark.django_db
def test_ingest_boot_key_no_audit_spam(fm, station_factory):
    station = station_factory()
    m = _ingest(station, boot_key="e30466f6b8470c1f")
    baseline = StationAuditLog.objects.filter(module=m).count()
    for _ in range(5):
        _ingest(station, boot_key="e30466f6b8470c1f")
    assert StationAuditLog.objects.filter(module=m).count() == baseline
