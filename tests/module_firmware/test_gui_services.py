import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from apps.accounts.models import AccountAuditLog
from apps.module_firmware import services
from apps.module_firmware.audit import audit_config, audit_station
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import StationAuditLog

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(username="staff", password="x", membership_level="staff")


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _request(user):
    req = RequestFactory().get("/")
    req.user = user
    return req


def _release(fm, version="26.09.15-01", variant="vhf"):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant=variant,
        version=version,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )


def _fleet_target(fm, version="26.09.15-01"):
    return ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=version
    )


def test_audit_config_writes_config_changed(user):
    audit_config(
        _request(user), obj="module_type", action="create", message="msg", details={"key": "fm"}
    )
    row = AccountAuditLog.objects.get(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)
    assert row.actor == user
    assert row.message == "msg"
    assert row.details["object"] == "module_type"
    assert row.details["action"] == "create"
    assert row.details["key"] == "fm"


def test_audit_station_skips_without_subject(user):
    audit_station(
        _request(user), event_type=StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED, message="x"
    )
    assert not StationAuditLog.objects.exists()


def test_reconcile_module_type_only_assigned_modules(fm, station_factory):
    st = station_factory()
    assigned = Module.objects.create(
        uid="A", module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=assigned, station=st, slot="slot0")
    Module.objects.create(uid="B", module_type=fm, variant="vhf", last_reported_version="old")
    _fleet_target(fm)
    _release(fm)
    assert services.reconcile_module_type(fm) == 1
    assigned.refresh_from_db()
    assert assigned.firmware_convergence == Module.Convergence.UPDATING


def test_release_quarantine_resets_state_and_audits(fm, station_factory, user):
    st = station_factory()
    m = Module.objects.create(
        uid="Q", module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    _fleet_target(fm)
    rel = _release(fm)
    cs = ModuleFirmwareConvergenceState.objects.create(
        module=m,
        target_release=rel,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
        attempts=3,
        last_error_mode="rejected",
        last_error_message="boom",
    )
    assert services.release_quarantine(m, user=user, ip_address="10.0.0.1") == [rel.version]
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    assert cs.attempts == 0
    assert cs.last_error_mode == ""
    assert cs.last_error_message == ""
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING
    row = StationAuditLog.objects.get(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    )
    assert row.user == user
    assert row.station == st
    assert row.module == m
    assert row.changes == {"released_targets": [rel.version]}


def test_release_quarantine_noop_when_none(fm, user):
    m = Module.objects.create(uid="N", module_type=fm, variant="vhf")
    assert services.release_quarantine(m, user=user) == []
    assert not StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    ).exists()
