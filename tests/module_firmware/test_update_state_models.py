import pytest

from apps.module_firmware.models import (
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleType,
)
from apps.stations.models import StationAuditLog


def test_new_convergence_choices():
    states = ModuleFirmwareConvergenceState.State
    assert {states.FAILED, states.NOT_UPDATABLE} <= set(states.values)
    assert {"failed", "not_updatable"} <= set(Module.Convergence.values)
    assert set(ModuleFirmwareConvergenceState.Phase.values) == {
        "downloading",
        "flashing",
        "verifying",
    }
    assert set(ModuleFirmwareConvergenceState.ReasonCode.values) == {
        "transient_limit",
        "boot_key_mismatch",
    }


@pytest.mark.django_db
def test_new_fields_default_blank():
    mt = ModuleType.objects.create(key="fm", display_name="FM")
    m = Module.objects.create(uid="u1", module_type=mt)
    rel = ModuleFirmwareRelease.objects.create(
        module_type=mt,
        version="1",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="o/r",
        source_tag="v1",
    )
    cs = ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)
    assert (cs.phase, cs.phase_at, cs.reason_code, cs.instructed_at) == ("", None, "", None)
    assert m.reported_boot_key == "" and rel.boot_key_hash == ""


def test_new_audit_event_types_fit_column():
    for name in [
        "MODULE_FLASH_INSTRUCTED",
        "MODULE_FLASH_PHASE",
        "MODULE_FLASH_CONFLICT",
        "MODULE_FLASH_GAVE_UP",
        "MODULE_NOT_UPDATABLE",
        "MODULE_UPDATABLE_AGAIN",
        "MODULE_FLASH_RETRY",
    ]:
        assert len(getattr(StationAuditLog.EventType, name).value) <= 32
