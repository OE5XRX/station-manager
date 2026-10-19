"""Write API (/api/v1/modules/) — notes/lifecycle PATCH + registration and
quarantine actions (``modules.manage``)."""

import pytest
from django.urls import reverse

from apps.api.models import PersonalAccessToken
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import StationAuditLog
from tests.test_api_read_fixtures import session

pytestmark = pytest.mark.django_db

LIST = "api:module-list"
DETAIL = "api:module-detail"
CONFIRM = "api:module-confirm-registration"
RELEASE = "api:module-release-quarantine"
TARGET = "26.09.15-01"

DENIED = [
    ("station_user", (403,)),
    ("applicant", (403,)),
    ("anon", (401, 403)),
]


@pytest.fixture
def fm():
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture
def module(fm):
    return Module.objects.create(uid="M1", module_type=fm, variant="vhf")


def _client(api_topology, bearer, anon_client, role):
    if role == "anon":
        return anon_client
    if role == "applicant":
        return session(api_topology["applicant"])
    return bearer(api_topology[role])


def _prefix(user):
    return PersonalAccessToken.objects.get(user=user).prefix


def _module_audits():
    # Fixtures (stations, assignments) write their own audit rows; only
    # module-subject rows are this endpoint's.
    return StationAuditLog.objects.filter(module__isnull=False)


def _assign(module, station):
    return ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot0")


def _quarantined(fm, station):
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=TARGET,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    m = Module.objects.create(
        uid="Q1", module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    _assign(m, station)
    Module.objects.filter(pk=m.pk).update(firmware_convergence=Module.Convergence.QUARANTINED)
    cs = ModuleFirmwareConvergenceState.objects.create(
        module=m,
        target_release=rel,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
        attempts=3,
        last_error_mode=ModuleFirmwareConvergenceState.ErrorMode.REJECTED,
        last_error_message="signature mismatch",
    )
    return m, cs


# --------------------------------------------------------------------- PATCH


@pytest.mark.parametrize("role", ["staff", "admin"])
def test_patch_lifecycle_allowed(api_topology, bearer, module, role):
    user = api_topology[role]
    client = bearer(user)
    resp = client.patch(reverse(DETAIL, args=[module.pk]), {"lifecycle_status": "defect"})
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["lifecycle_status"] == "defect"
    assert body["uid"] == "M1"
    assert "firmware_convergence" in body
    module.refresh_from_db()
    assert module.lifecycle_status == "defect"
    (log,) = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED
    )
    assert log.module == module
    assert log.user == user
    assert log.message.endswith(f"via API token {_prefix(user)}")


@pytest.mark.parametrize("role,expected", DENIED)
def test_patch_forbidden(api_topology, bearer, anon_client, module, role, expected):
    client = _client(api_topology, bearer, anon_client, role)
    resp = client.patch(
        reverse(DETAIL, args=[module.pk]),
        {"lifecycle_status": "defect", "notes": "x"},
        format="json",
    )
    assert resp.status_code in expected
    module.refresh_from_db()
    assert module.lifecycle_status == Module.Lifecycle.READY
    assert module.notes == ""
    assert not _module_audits().exists()


def test_patch_deployed_rejected(api_topology, bearer, module):
    client = bearer(api_topology["staff"])
    resp = client.patch(
        reverse(DETAIL, args=[module.pk]), {"lifecycle_status": "deployed"}, format="json"
    )
    assert resp.status_code == 400
    assert "lifecycle_status" in resp.json()
    module.refresh_from_db()
    assert module.lifecycle_status == Module.Lifecycle.READY


def test_patch_ready_with_open_assignment_rejected_atomically(api_topology, bearer, module):
    _assign(module, api_topology["station_in"])
    Module.objects.filter(pk=module.pk).update(lifecycle_status=Module.Lifecycle.DEFECT)
    client = bearer(api_topology["staff"])
    resp = client.patch(
        reverse(DETAIL, args=[module.pk]),
        {"lifecycle_status": "ready", "notes": "should not stick"},
        format="json",
    )
    assert resp.status_code == 400
    assert "lifecycle_status" in resp.json()
    module.refresh_from_db()
    assert module.lifecycle_status == Module.Lifecycle.DEFECT
    assert module.notes == ""
    assert not _module_audits().exists()


def test_patch_notes_audited_with_station(api_topology, bearer, module):
    station = api_topology["station_in"]
    _assign(module, station)
    staff = api_topology["staff"]
    client = bearer(staff)
    resp = client.patch(reverse(DETAIL, args=[module.pk]), {"notes": "new note"}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["notes"] == "new note"
    module.refresh_from_db()
    assert module.notes == "new note"
    (log,) = StationAuditLog.objects.filter(event_type=StationAuditLog.EventType.UPDATED)
    assert log.module == module
    assert log.station == station
    assert log.user == staff
    assert log.changes == {"notes": {"old": "", "new": "new note"}}
    assert log.message.endswith(f"via API token {_prefix(staff)}")


def test_patch_notes_unassigned_module_audited_without_station(api_topology, bearer, module):
    client = bearer(api_topology["staff"])
    resp = client.patch(reverse(DETAIL, args=[module.pk]), {"notes": "n"}, format="json")
    assert resp.status_code == 200
    (log,) = StationAuditLog.objects.filter(event_type=StationAuditLog.EventType.UPDATED)
    assert log.station is None
    assert log.module == module


def test_patch_unchanged_notes_no_audit(api_topology, bearer, module):
    Module.objects.filter(pk=module.pk).update(notes="same")
    client = bearer(api_topology["staff"])
    resp = client.patch(reverse(DETAIL, args=[module.pk]), {"notes": "same"}, format="json")
    assert resp.status_code == 200
    assert not _module_audits().exists()


def test_patch_notes_whitespace_only_edit_applied_and_audited(api_topology, bearer, module):
    """Notes are stored raw (like the GUI form): a trailing-space edit is a
    real change, not stripped back to the old value and silently dropped."""
    Module.objects.filter(pk=module.pk).update(notes="same")
    client = bearer(api_topology["staff"])
    resp = client.patch(reverse(DETAIL, args=[module.pk]), {"notes": "same "}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["notes"] == "same "
    module.refresh_from_db()
    assert module.notes == "same "
    (log,) = _module_audits()
    assert log.changes == {"notes": {"old": "same", "new": "same "}}


def test_put_not_allowed(api_topology, bearer, module):
    client = bearer(api_topology["staff"])
    resp = client.put(reverse(DETAIL, args=[module.pk]), {"notes": "x"}, format="json")
    assert resp.status_code == 405


def test_delete_and_create_not_allowed(api_topology, bearer, module):
    client = bearer(api_topology["staff"])
    assert client.delete(reverse(DETAIL, args=[module.pk])).status_code == 405
    assert client.post(reverse(LIST), {"uid": "X"}, format="json").status_code == 405
    assert Module.objects.filter(pk=module.pk).exists()


# ---------------------------------------------------------- confirm-registration


@pytest.mark.parametrize("role", ["staff", "admin"])
def test_confirm_registration_idempotent(api_topology, bearer, module, role):
    user = api_topology[role]
    client = bearer(user)
    resp = client.post(reverse(CONFIRM, args=[module.pk]))
    assert resp.status_code == 200, resp.content
    assert resp.json()["registration_status"] == "registered"
    logs = StationAuditLog.objects.filter(event_type=StationAuditLog.EventType.MODULE_REGISTERED)
    (log,) = logs
    assert log.user == user
    assert log.message.endswith(f"via API token {_prefix(user)}")

    resp = client.post(reverse(CONFIRM, args=[module.pk]))
    assert resp.status_code == 200
    assert logs.count() == 1


@pytest.mark.parametrize("role,expected", DENIED)
def test_confirm_registration_forbidden(api_topology, bearer, anon_client, module, role, expected):
    client = _client(api_topology, bearer, anon_client, role)
    resp = client.post(reverse(CONFIRM, args=[module.pk]))
    assert resp.status_code in expected
    module.refresh_from_db()
    assert module.registration_status == Module.Registration.UNREGISTERED
    assert not _module_audits().exists()


def test_confirm_registration_unknown_404(api_topology, bearer):
    client = bearer(api_topology["staff"])
    assert client.post(reverse(CONFIRM, args=[999999])).status_code == 404


# ---------------------------------------------------------- release-quarantine


@pytest.mark.parametrize("role", ["staff", "admin"])
def test_release_quarantine(api_topology, bearer, fm, role):
    station = api_topology["station_in"]
    m, cs = _quarantined(fm, station)
    user = api_topology[role]
    client = bearer(user)
    resp = client.post(reverse(RELEASE, args=[m.pk]))
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["released_targets"] == [TARGET]
    assert body["module"]["uid"] == "Q1"
    assert body["module"]["firmware_convergence"] == Module.Convergence.UPDATING
    assert body["module"]["quarantine"] is None
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    assert cs.attempts == 0
    (log,) = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    )
    assert log.user == user
    assert log.module == m
    assert log.station == station
    assert log.message.endswith(f"via API token {_prefix(user)}")


def test_release_quarantine_not_quarantined_noop(api_topology, bearer, module):
    client = bearer(api_topology["staff"])
    resp = client.post(reverse(RELEASE, args=[module.pk]))
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["released_targets"] == []
    assert body["module"]["uid"] == "M1"
    assert not StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    ).exists()


@pytest.mark.parametrize("role,expected", DENIED)
def test_release_quarantine_forbidden(api_topology, bearer, anon_client, fm, role, expected):
    m, cs = _quarantined(fm, api_topology["station_in"])
    client = _client(api_topology, bearer, anon_client, role)
    resp = client.post(reverse(RELEASE, args=[m.pk]))
    assert resp.status_code in expected
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    assert not _module_audits().exists()
