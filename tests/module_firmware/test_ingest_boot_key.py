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


@pytest.mark.django_db
def test_malformed_boot_key_warning_is_bounded(fm, station_factory, caplog):
    import logging

    huge = "Z" * 10_000
    with caplog.at_level(logging.WARNING, logger="apps.module_firmware.ingest"):
        _ingest(station_factory(), boot_key=huge)
    [rec] = [r for r in caplog.records if "malformed boot_key" in r.getMessage()]
    msg = rec.getMessage()
    assert "Z" * 100 not in msg
    assert len(msg) < 200
    assert "uid='BK1'" in msg


# --- review round 2: agent-supplied uid is logged bounded + repr'd ----------------------


@pytest.mark.django_db
def test_unknown_type_log_bounds_and_escapes_uid(station_factory, caplog):
    uid = "EVIL\nFORGED " + "x" * 200
    with caplog.at_level("WARNING", logger="apps.module_firmware.ingest"):
        out = ingest_module(station_factory(), "slot1", "nope", ident(uid=uid), now=timezone.now())
    assert out is None
    msg = caplog.records[-1].getMessage()
    assert "\n" not in msg
    assert repr(uid)[:64] in msg
    assert "x" * 100 not in msg


@pytest.mark.django_db
def test_malformed_boot_key_log_bounds_and_escapes_uid(fm, station_factory, caplog):
    uid = "EVIL\nFORGED"
    with caplog.at_level("WARNING", logger="apps.module_firmware.ingest"):
        _ingest(station_factory(), uid=uid, boot_key="xyz")
    msgs = [r.getMessage() for r in caplog.records if "malformed boot_key" in r.getMessage()]
    assert msgs and "\n" not in msgs[-1]
    assert repr(uid)[:64] in msgs[-1]
