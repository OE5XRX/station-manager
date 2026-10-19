from datetime import timedelta

import pytest
from django.utils import timezone

from apps.module_firmware.drain import (
    DrainStatus,
    artifact_available,
    artifact_for,
    drain_status,
    is_draining,
    pending_updates,
)
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)

State = ModuleFirmwareConvergenceState.State


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _online(station, backends=("dfu",)):
    station.last_seen = timezone.now()
    station.agent_capabilities = {"module_ota": {"backends": list(backends)}} if backends else {}
    station.save(update_fields=["last_seen", "agent_capabilities"])
    return station


def _target(fm, version="26.09.15-01"):
    return ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=version
    )


def _release(fm, version="26.09.15-01", *, sim=False):
    kwargs = {}
    if sim:
        kwargs = {"sim_storage_key": "sk", "sim_sha256": "b" * 64, "sim_size_bytes": 456}
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=version,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=123,
        source_repo="r",
        source_tag="t",
        **kwargs,
    )


def _module(fm, station, *, uid="U1", slot="slot0", synthetic=False):
    m = Module.objects.create(
        uid=uid,
        module_type=fm,
        variant="vhf",
        last_reported_version="26.09.10-01",
        uid_source=Module.UidSource.SYNTHETIC if synthetic else Module.UidSource.STM32_UID,
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    return m


def _row(m, rel, state=State.UPDATING):
    return ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel, state=state)


def test_artifact_for(fm, station):
    assert artifact_for(_module(fm, station, uid="R")) == "dfu"
    assert artifact_for(_module(fm, station, uid="S", slot="slot1", synthetic=True)) == "sim"


def test_artifact_available(fm):
    rel = _release(fm)
    assert artifact_available(rel, "dfu") is True
    assert artifact_available(rel, "sim") is False
    rel2 = _release(fm, version="26.09.16-01", sim=True)
    assert artifact_available(rel2, "sim") is True
    assert artifact_available(rel2, "bogus") is False


def test_drain_active_for_pending_dfu(fm, station):
    _online(station)
    _target(fm)
    rel = _release(fm)
    m = _module(fm, station)
    cs = _row(m, rel)
    st = drain_status(station)
    assert isinstance(st, DrainStatus)
    assert st.active is True
    assert st.module_uids == ("U1",)
    assert st.since == cs.created_at
    assert is_draining(station) is True
    assert pending_updates(station) == [cs]


def test_no_drain_without_capability(fm, station):
    _online(station, backends=())
    _target(fm)
    _row(_module(fm, station), _release(fm))
    assert is_draining(station) is False
    assert drain_status(station) == DrainStatus(active=False, since=None, module_uids=())


def test_no_drain_when_offline(fm, station):
    _online(station)
    station.last_seen = timezone.now() - timedelta(minutes=10)
    station.save(update_fields=["last_seen"])
    _target(fm)
    _row(_module(fm, station), _release(fm))
    assert is_draining(station) is False
    assert drain_status(station).active is False


def test_no_drain_when_quarantined(fm, station):
    _online(station)
    _target(fm)
    _row(_module(fm, station), _release(fm), state=State.QUARANTINED)
    assert is_draining(station) is False


def test_no_drain_when_converged(fm, station):
    _online(station)
    _target(fm)
    _row(_module(fm, station), _release(fm), state=State.OK)
    assert is_draining(station) is False


def test_sim_module_without_sim_artifact_no_drain_no_instruction(fm, station):
    _online(station, backends=("dfu", "sim"))
    _target(fm)
    _row(_module(fm, station, synthetic=True), _release(fm, sim=False))
    assert pending_updates(station) == []
    assert is_draining(station) is False


def test_sim_module_with_sim_artifact_drains(fm, station):
    _online(station, backends=("sim",))
    _target(fm)
    _row(_module(fm, station, synthetic=True), _release(fm, sim=True))
    assert is_draining(station) is True


def test_drain_since_is_oldest_pending_created_at(fm, station):
    _online(station)
    _target(fm)
    rel = _release(fm)
    a = _row(_module(fm, station, uid="A", slot="slot0"), rel)
    b = _row(_module(fm, station, uid="B", slot="slot1"), rel)
    old = timezone.now() - timedelta(hours=3)
    ModuleFirmwareConvergenceState.objects.filter(pk=b.pk).update(created_at=old)
    st = drain_status(station)
    assert st.since == old
    assert st.module_uids == ("A", "B")  # slot order
    assert [r.pk for r in pending_updates(station)] == [a.pk, b.pk]


def test_stale_row_for_old_target_ignored(fm, station):
    _online(station)
    _target(fm, version="26.09.20-01")
    old_rel = _release(fm, version="26.09.15-01")
    _release(fm, version="26.09.20-01")
    _row(_module(fm, station), old_rel)
    assert is_draining(station) is False


def test_closed_assignment_ignored(fm, station):
    _online(station)
    _target(fm)
    m = _module(fm, station)
    _row(m, _release(fm))
    ModuleAssignmentHistory.objects.filter(module=m).update(to_ts=timezone.now())
    assert is_draining(station) is False


def test_drain_ends_after_transient_failure_cap(fm, station):
    from apps.module_firmware.models import DRAIN_TRANSIENT_LIMIT

    _online(station)
    _target(fm)
    rel = _release(fm)
    m = _module(fm, station)
    cs = _row(m, rel)
    cs.transient_failures = DRAIN_TRANSIENT_LIMIT - 1
    cs.save(update_fields=["transient_failures"])
    assert is_draining(station)
    cs.transient_failures = DRAIN_TRANSIENT_LIMIT
    cs.save(update_fields=["transient_failures"])
    assert not is_draining(station)
    assert pending_updates(station) == []
    cs.refresh_from_db()
    assert cs.state == State.UPDATING  # no quarantine — the check still offers it


def test_reconcile_ok_resets_transient_failures(fm, station):
    from apps.module_firmware.reconciler import reconcile_module

    _target(fm)
    rel = _release(fm)
    m = _module(fm, station)
    cs = _row(m, rel)
    cs.transient_failures = 3
    cs.save(update_fields=["transient_failures"])
    m.last_reported_version = rel.version
    m.save(update_fields=["last_reported_version"])
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == State.OK
    assert cs.transient_failures == 0
