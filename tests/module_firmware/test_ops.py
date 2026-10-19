"""Shared module-firmware operations (used by the GUI views and the REST API)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from apps.accounts.models import AccountAuditLog
from apps.module_firmware import ops, services
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareImportJob,
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
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )


def _request(user):
    req = RequestFactory().post("/")
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


def _config_logs(obj):
    return AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED, details__object=obj
    )


def test_queue_import_creates_then_dedups(fm, user):
    job, created = ops.queue_import(_request(user), fm.pk, "v1.0.0")
    assert created is True
    assert job.status == ModuleFirmwareImportJob.Status.PENDING
    assert job.requested_by == user
    assert job.source_repo == fm.firmware_repo

    again, created2 = ops.queue_import(_request(user), fm.pk, "v1.0.0")
    assert created2 is False
    assert again == job
    assert ModuleFirmwareImportJob.objects.count() == 1
    assert _config_logs("module_firmware_import").count() == 1


def test_queue_import_rejects_invalid_tag(fm, user):
    with pytest.raises(ops.ImportRejected) as exc:
        ops.queue_import(_request(user), fm.pk, "../evil")
    assert exc.value.field == "tag"
    assert exc.value.code == "invalid_tag"
    assert not ModuleFirmwareImportJob.objects.exists()


def test_queue_import_rejects_not_ready_type(db, user):
    bare = ModuleType.objects.create(key="bare", display_name="Bare")
    with pytest.raises(ops.ImportRejected) as exc:
        ops.queue_import(_request(user), bare.pk, "v1.0.0")
    assert exc.value.field == "module_type"
    assert exc.value.code == "not_ready"
    assert str(exc.value)
    assert not ModuleFirmwareImportJob.objects.exists()


def test_queue_import_unknown_type_raises_does_not_exist(db, user):
    with pytest.raises(ModuleType.DoesNotExist):
        ops.queue_import(_request(user), 999999, "v1.0.0")


def test_archive_release_idempotent_audits_once(fm, user):
    rel = _release(fm)
    assert ops.archive_release(_request(user), rel) is True
    assert ops.archive_release(_request(user), rel) is False
    assert _config_logs("module_firmware_release").filter(details__action="archived").count() == 1

    assert ops.restore_release(_request(user), rel) is True
    assert ops.restore_release(_request(user), rel) is False
    assert _config_logs("module_firmware_release").filter(details__action="restored").count() == 1


def test_after_target_updated_noop_returns_false(fm, user):
    target = ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    before = ModuleFirmwareTarget.objects.get(pk=target.pk)
    assert ops.after_target_updated(_request(user), before, target) is False
    assert not _config_logs("module_firmware_target").exists()

    target.version = "26.09.16-01"
    target.save()
    assert ops.after_target_updated(_request(user), before, target) is True
    row = _config_logs("module_firmware_target").get()
    assert row.details["changes"] == {"version": {"old": "26.09.15-01", "new": "26.09.16-01"}}


def test_via_suffix_on_audit_messages(fm, user, station_factory):
    rel = _release(fm)
    via = " via API token abc"
    ops.archive_release(_request(user), rel, via=via)
    assert _config_logs("module_firmware_release").get().message.endswith(via)

    st = station_factory()
    target = ModuleFirmwareTarget.objects.create(
        module_type=fm,
        scope=ModuleFirmwareTarget.Scope.STATION,
        station=st,
        version="26.09.15-01",
    )
    ops.after_target_created(_request(user), target, via=via)
    assert _config_logs("module_firmware_target").get().message.endswith(via)
    station_row = StationAuditLog.objects.get(
        event_type=StationAuditLog.EventType.FIRMWARE_TARGET_SET
    )
    assert station_row.message.endswith(via)


def test_release_quarantine_audit_suffix(fm, station_factory, user):
    st = station_factory()
    m = Module.objects.create(
        uid="Q",
        module_type=fm,
        variant="vhf",
        last_reported_version="26.09.10-01",
        firmware_convergence=Module.Convergence.QUARANTINED,
    )
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = _release(fm)
    ModuleFirmwareConvergenceState.objects.create(
        module=m,
        target_release=rel,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
        attempts=3,
    )
    suffix = " via API token abc"
    assert services.release_quarantine(m, user=user, audit_suffix=suffix) == [rel.version]
    row = StationAuditLog.objects.get(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    )
    assert row.message.endswith(suffix)


def test_confirm_registration_and_set_lifecycle_audit_suffix(fm, user):
    m = Module.objects.create(uid="R", module_type=fm, variant="vhf")
    suffix = " via API token abc"
    services.confirm_registration(m, user=user, ip_address="10.0.0.2", audit_suffix=suffix)
    reg = StationAuditLog.objects.get(event_type=StationAuditLog.EventType.MODULE_REGISTERED)
    assert reg.message.endswith(suffix)
    assert reg.ip_address == "10.0.0.2"

    services.set_lifecycle(
        m, Module.Lifecycle.RETIRED, user=user, ip_address="10.0.0.2", audit_suffix=suffix
    )
    lc = StationAuditLog.objects.get(event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED)
    assert lc.message.endswith(suffix)
    assert lc.ip_address == "10.0.0.2"


def test_after_target_updated_cross_type_move_reconciles_both_types(fm, user, monkeypatch):
    other = ModuleType.objects.create(key="pwr", display_name="Power")
    target = ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    before = ModuleFirmwareTarget.objects.select_related("module_type").get(pk=target.pk)
    reconciled = []
    monkeypatch.setattr(ops, "reconcile_module_type", lambda mt: reconciled.append(mt.key))

    target.module_type = other
    target.save()
    assert ops.after_target_updated(_request(user), before, target) is True
    assert sorted(reconciled) == ["fm", "pwr"]

    reconciled.clear()
    target.version = "26.09.16-01"
    target.save()
    same_type_before = ModuleFirmwareTarget.objects.select_related("module_type").get(pk=target.pk)
    same_type_before.version = "26.09.15-01"
    assert ops.after_target_updated(_request(user), same_type_before, target) is True
    assert reconciled == ["pwr"]
