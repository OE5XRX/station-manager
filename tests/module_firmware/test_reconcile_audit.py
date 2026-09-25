import pytest

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.reconciler import reconcile_module, record_error
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def test_new_event_types_exist():
    for name in [
        "FIRMWARE_TARGET_SET",
        "MODULE_FLASH_STARTED",
        "MODULE_FLASH_SUCCESS",
        "MODULE_FLASH_ROLLED_BACK",
        "MODULE_FLASH_REJECTED",
        "MODULE_FLASH_FAILED",
        "MODULE_QUARANTINED",
    ]:
        assert hasattr(StationAuditLog.EventType, name)


@pytest.mark.django_db
def test_quarantine_emits_audit_once(fm, station_factory):
    st = station_factory()
    m = Module.objects.create(
        uid="Q1", module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
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
    cs = reconcile_module(m)
    record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.REJECTED)
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.MODULE_QUARANTINED
        ).count()
        == 1
    )
