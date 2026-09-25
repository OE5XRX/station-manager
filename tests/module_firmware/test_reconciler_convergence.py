import pytest

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
    QUARANTINE_ATTEMPT_LIMIT,
)
from apps.module_firmware.reconciler import reconcile_module, record_error


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _module_with_target(fm, station, *, variant="vhf", version="", target="26.09.15-01"):
    m = Module.objects.create(
        uid="M1", module_type=fm, variant=variant, last_reported_version=version
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot="slot0")
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=target
    )
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant=variant, version=target,
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    return m


@pytest.mark.django_db
def test_reported_equals_desired_is_ok(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.15-01")
    cs = reconcile_module(m)
    assert cs.state == ModuleFirmwareConvergenceState.State.OK
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK


@pytest.mark.django_db
def test_reported_differs_is_updating(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING


@pytest.mark.django_db
def test_no_desired_no_drift(fm, station_factory):
    st = station_factory()
    m = Module.objects.create(uid="ND", module_type=fm, variant="vhf", last_reported_version="x")
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    assert reconcile_module(m) is None
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UNKNOWN


@pytest.mark.django_db
def test_reconcile_is_idempotent(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    reconcile_module(m)
    reconcile_module(m)
    assert ModuleFirmwareConvergenceState.objects.filter(module=m).count() == 1


@pytest.mark.django_db
def test_rejected_immediate_quarantine(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    cs = record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.REJECTED)
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    assert cs.attempts == 0
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.QUARANTINED


@pytest.mark.django_db
def test_rolled_back_quarantines_at_limit(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    for _ in range(QUARANTINE_ATTEMPT_LIMIT - 1):
        cs = record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK)
        assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    cs = record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK)
    assert cs.attempts == QUARANTINE_ATTEMPT_LIMIT
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED


@pytest.mark.django_db
def test_transient_does_not_count(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    for _ in range(5):
        cs = record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.TRANSIENT)
    assert cs.attempts == 0
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING


@pytest.mark.django_db
def test_new_target_version_new_row_retried(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.REJECTED)
    # Operator ships a fix: new target version + release -> new row, retried.
    ModuleFirmwareTarget.objects.filter(module_type=fm).update(version="26.09.20-01")
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.20-01",
        storage_key="k2", sha256="c" * 64, size_bytes=1, source_repo="r", source_tag="t2",
    )
    cs2 = reconcile_module(m)
    assert cs2.pk != cs.pk
    assert cs2.state == ModuleFirmwareConvergenceState.State.UPDATING
    assert ModuleFirmwareConvergenceState.objects.filter(module=m).count() == 2
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING


@pytest.mark.django_db
def test_quarantined_row_not_reactivated_on_reconcile(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.REJECTED)
    cs2 = reconcile_module(m)  # same target still drifted
    assert cs2.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.QUARANTINED
