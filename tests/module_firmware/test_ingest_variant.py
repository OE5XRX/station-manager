# tests/module_firmware/test_ingest_variant.py
import pytest
from django.utils import timezone

from apps.module_firmware.ingest import ingest_module
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(**kw):
    base = {"type": "fm", "model": "SA818", "version": "26.09.10-01", "uid": "UID1"}
    base.update(kw)
    return base


@pytest.mark.django_db
def test_variant_captured_at_discovery(fm, station_factory):
    st = station_factory()
    m = ingest_module(st, "slot0", "fm", ident(variant="vhf"), now=timezone.now())
    assert m.variant == "vhf"


@pytest.mark.django_db
def test_variant_immutable_change_is_anomaly(fm, station_factory):
    st = station_factory()
    ingest_module(st, "slot0", "fm", ident(variant="vhf"), now=timezone.now())
    for _ in range(3):
        ingest_module(st, "slot0", "fm", ident(variant="uhf"), now=timezone.now())
    m = Module.objects.get(uid="UID1")
    assert m.variant == "vhf"  # unchanged
    # Audited once, not per heartbeat.
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.UPDATED, message__icontains="variant"
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_variant_blank_then_nonblank_is_set_not_anomaly(fm, station_factory):
    # Review Focus #1: blank at discovery, non-blank later -> fill once, no anomaly.
    st = station_factory()
    ingest_module(st, "slot0", "fm", ident(variant=""), now=timezone.now())
    ingest_module(st, "slot0", "fm", ident(variant="vhf"), now=timezone.now())
    m = Module.objects.get(uid="UID1")
    assert m.variant == "vhf"
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.UPDATED, message__icontains="variant"
        ).count()
        == 0
    )


@pytest.mark.django_db
def test_ingest_triggers_reconcile(fm, station_factory):
    st = station_factory()
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    # Discovery reports an old version -> drift -> updating.
    m = ingest_module(
        st, "slot0", "fm", ident(variant="vhf", version="26.09.10-01"), now=timezone.now()
    )
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING
    assert ModuleFirmwareConvergenceState.objects.filter(module=m).count() == 1
