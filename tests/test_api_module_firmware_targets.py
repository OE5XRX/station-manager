"""Write API (/api/v1/module-firmware-targets/) — create/update/delete
(``modules.manage``)."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountAuditLog
from apps.api.models import PersonalAccessToken
from apps.module_firmware import ops
from apps.module_firmware.models import (
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import RolloutGroup, StationAuditLog
from tests.test_api_read_fixtures import session

pytestmark = pytest.mark.django_db

LIST = "api:module-firmware-target-list"
DETAIL = "api:module-firmware-target-detail"


@pytest.fixture
def fm():
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )


def _release(module_type, version, variant="vhf"):
    return ModuleFirmwareRelease.objects.create(
        module_type=module_type,
        variant=variant,
        version=version,
        storage_key=f"k-{version}-{variant}",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="OE5XRX/FW-RemoteStation",
        source_tag=f"v{version}",
    )


def _archive(release):
    ModuleFirmwareRelease.all_objects.filter(pk=release.pk).update(archived_at=timezone.now())


@pytest.fixture
def rel1(fm):
    return _release(fm, "1.0")


@pytest.fixture
def rel2(fm):
    return _release(fm, "2.0")


@pytest.fixture
def group():
    return RolloutGroup.objects.create(name="Canary", slug="canary")


@pytest.fixture
def reconciled(monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "reconcile_module_type", lambda mt: calls.append(mt.key))
    return calls


@pytest.fixture
def fleet_target(fm, rel1, api_topology):
    return ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1.0", scope="fleet", created_by=api_topology["staff"]
    )


def _audits():
    return AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        details__object="module_firmware_target",
    )


def _station_audits(event_type):
    return StationAuditLog.objects.filter(event_type=event_type)


def _client(api_topology, bearer, anon_client, role):
    if role == "anon":
        return anon_client
    if role == "applicant":
        return session(api_topology["applicant"])
    return bearer(api_topology[role])


# --- permission matrix ------------------------------------------------------


@pytest.mark.parametrize("role", ["staff", "admin"])
def test_create_allowed_for_staff_and_admin(api_topology, bearer, rel1, reconciled, role):
    resp = bearer(api_topology[role]).post(
        reverse(LIST), {"release": rel1.pk, "scope": "fleet"}, format="json"
    )
    assert resp.status_code == 201, resp.content
    assert ModuleFirmwareTarget.objects.get().created_by == api_topology[role]


@pytest.mark.parametrize(
    "role,expected",
    [("station_user", (403,)), ("applicant", (403,)), ("anon", (401, 403))],
)
def test_create_forbidden_below_staff(
    api_topology, bearer, anon_client, rel1, reconciled, role, expected
):
    client = _client(api_topology, bearer, anon_client, role)
    resp = client.post(reverse(LIST), {"release": rel1.pk, "scope": "fleet"}, format="json")
    assert resp.status_code in expected
    assert not ModuleFirmwareTarget.objects.exists()
    assert not _audits().exists()
    assert reconciled == []


@pytest.mark.parametrize("role", ["staff", "admin"])
def test_update_allowed_for_staff_and_admin(
    api_topology, bearer, fleet_target, rel2, reconciled, role
):
    resp = bearer(api_topology[role]).patch(
        reverse(DETAIL, args=[fleet_target.pk]), {"release": rel2.pk}, format="json"
    )
    assert resp.status_code == 200, resp.content
    fleet_target.refresh_from_db()
    assert fleet_target.version == "2.0"


@pytest.mark.parametrize(
    "role,expected",
    [("station_user", (403,)), ("applicant", (403,)), ("anon", (401, 403))],
)
def test_update_forbidden_below_staff(
    api_topology, bearer, anon_client, fleet_target, rel2, reconciled, role, expected
):
    client = _client(api_topology, bearer, anon_client, role)
    resp = client.patch(
        reverse(DETAIL, args=[fleet_target.pk]), {"release": rel2.pk}, format="json"
    )
    assert resp.status_code in expected
    fleet_target.refresh_from_db()
    assert fleet_target.version == "1.0"
    assert not _audits().exists()


@pytest.mark.parametrize("role", ["staff", "admin"])
def test_delete_allowed_for_staff_and_admin(api_topology, bearer, fleet_target, reconciled, role):
    resp = bearer(api_topology[role]).delete(reverse(DETAIL, args=[fleet_target.pk]))
    assert resp.status_code == 204
    assert not ModuleFirmwareTarget.objects.exists()


@pytest.mark.parametrize(
    "role,expected",
    [("station_user", (403,)), ("applicant", (403,)), ("anon", (401, 403))],
)
def test_delete_forbidden_below_staff(
    api_topology, bearer, anon_client, fleet_target, reconciled, role, expected
):
    client = _client(api_topology, bearer, anon_client, role)
    resp = client.delete(reverse(DETAIL, args=[fleet_target.pk]))
    assert resp.status_code in expected
    assert ModuleFirmwareTarget.objects.filter(pk=fleet_target.pk).exists()
    assert not _audits().exists()


# --- create -----------------------------------------------------------------


def test_create_fleet_target_audits_and_reconciles(api_topology, bearer, rel1, reconciled):
    staff = api_topology["staff"]
    client = bearer(staff)
    prefix = PersonalAccessToken.objects.get(user=staff).prefix

    resp = client.post(reverse(LIST), {"release": rel1.pk, "scope": "fleet"}, format="json")

    assert resp.status_code == 201, resp.content
    body = resp.json()
    target = ModuleFirmwareTarget.objects.get()
    assert body["id"] == target.pk
    assert body["module_type"] == rel1.module_type_id
    assert body["module_type_key"] == "fm"
    assert body["version"] == "1.0"
    assert body["scope"] == "fleet"
    assert body["variant_coverage"] == ["vhf"]
    assert body["created_by"] == staff.username
    assert "release" not in body
    (log,) = _audits()
    assert log.details["action"] == "created"
    assert log.details["version"] == "1.0"
    assert log.message.endswith(f"via API token {prefix}")
    assert log.actor == staff
    assert reconciled == ["fm"]
    assert not StationAuditLog.objects.filter(event_type__startswith="firmware_target").exists()


def test_create_station_target_writes_station_audit(api_topology, bearer, rel1, reconciled):
    station = api_topology["station_in"]
    resp = bearer(api_topology["staff"]).post(
        reverse(LIST),
        {"release": rel1.pk, "scope": "station", "station": station.pk},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["station"] == station.pk
    (slog,) = _station_audits(StationAuditLog.EventType.FIRMWARE_TARGET_SET)
    assert slog.station == station
    assert "via API token" in slog.message
    assert _audits().count() == 1


def test_create_second_fleet_target_same_type_400(api_topology, bearer, fleet_target, rel2):
    resp = bearer(api_topology["staff"]).post(
        reverse(LIST), {"release": rel2.pk, "scope": "fleet"}, format="json"
    )
    assert resp.status_code == 400
    assert "non_field_errors" in resp.json()
    assert ModuleFirmwareTarget.objects.count() == 1
    assert not _audits().exists()


def test_create_group_scope_without_group_400(api_topology, bearer, rel1, reconciled):
    resp = bearer(api_topology["staff"]).post(
        reverse(LIST), {"release": rel1.pk, "scope": "group"}, format="json"
    )
    assert resp.status_code == 400
    assert "non_field_errors" in resp.json()
    assert not ModuleFirmwareTarget.objects.exists()
    assert reconciled == []


def test_create_canary_on_station_scope_400(api_topology, bearer, rel1, group, reconciled):
    resp = bearer(api_topology["staff"]).post(
        reverse(LIST),
        {
            "release": rel1.pk,
            "scope": "station",
            "station": api_topology["station_in"].pk,
            "canary_group": group.pk,
        },
        format="json",
    )
    assert resp.status_code == 400
    assert "non_field_errors" in resp.json()
    assert not ModuleFirmwareTarget.objects.exists()


def test_create_fleet_with_canary_group(api_topology, bearer, rel1, group, reconciled):
    resp = bearer(api_topology["staff"]).post(
        reverse(LIST),
        {"release": rel1.pk, "scope": "fleet", "canary_group": group.pk},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["canary_group"] == group.pk


def test_create_archived_release_400_on_release(api_topology, bearer, rel1, reconciled):
    _archive(rel1)
    resp = bearer(api_topology["staff"]).post(
        reverse(LIST), {"release": rel1.pk, "scope": "fleet"}, format="json"
    )
    assert resp.status_code == 400
    assert "release" in resp.json()
    assert not ModuleFirmwareTarget.objects.exists()


def test_create_nonexistent_release_400_on_release(api_topology, bearer, reconciled):
    resp = bearer(api_topology["staff"]).post(
        reverse(LIST), {"release": 999999, "scope": "fleet"}, format="json"
    )
    assert resp.status_code == 400
    assert "release" in resp.json()


def test_create_without_release_400(api_topology, bearer, reconciled):
    resp = bearer(api_topology["staff"]).post(reverse(LIST), {"scope": "fleet"}, format="json")
    assert resp.status_code == 400
    assert "release" in resp.json()


# --- update -----------------------------------------------------------------


def test_patch_release_audits_changes(api_topology, bearer, fleet_target, rel2, reconciled):
    resp = bearer(api_topology["staff"]).patch(
        reverse(DETAIL, args=[fleet_target.pk]), {"release": rel2.pk}, format="json"
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["version"] == "2.0"
    (log,) = _audits()
    assert log.details["action"] == "updated"
    assert log.details["changes"] == {"version": {"old": "1.0", "new": "2.0"}}
    assert "via API token" in log.message
    assert reconciled == ["fm"]


def test_patch_scope_only_on_archived_release_target_200(
    api_topology, bearer, fleet_target, rel1, reconciled
):
    _archive(rel1)
    station = api_topology["station_in"]
    resp = bearer(api_topology["staff"]).patch(
        reverse(DETAIL, args=[fleet_target.pk]),
        {"scope": "station", "station": station.pk},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    fleet_target.refresh_from_db()
    assert fleet_target.scope == "station"
    assert fleet_target.station == station
    assert fleet_target.version == "1.0"
    assert _station_audits(StationAuditLog.EventType.FIRMWARE_TARGET_SET).count() == 1


def test_patch_station_to_fleet_writes_removed_station_audit(
    api_topology, bearer, fm, rel1, reconciled
):
    station = api_topology["station_in"]
    target = ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1.0", scope="station", station=station
    )
    resp = bearer(api_topology["staff"]).patch(
        reverse(DETAIL, args=[target.pk]), {"scope": "fleet", "station": None}, format="json"
    )
    assert resp.status_code == 200, resp.content
    (slog,) = _station_audits(StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED)
    assert slog.station == station


def test_patch_noop_writes_no_audit(api_topology, bearer, fleet_target, rel1, reconciled):
    resp = bearer(api_topology["staff"]).patch(
        reverse(DETAIL, args=[fleet_target.pk]),
        {"release": rel1.pk, "scope": "fleet"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert not _audits().exists()
    assert reconciled == []


def test_patch_nonexistent_release_400(api_topology, bearer, fleet_target, reconciled):
    resp = bearer(api_topology["staff"]).patch(
        reverse(DETAIL, args=[fleet_target.pk]), {"release": 999999}, format="json"
    )
    assert resp.status_code == 400
    assert "release" in resp.json()


def test_put_requires_release(api_topology, bearer, fleet_target, reconciled):
    resp = bearer(api_topology["staff"]).put(
        reverse(DETAIL, args=[fleet_target.pk]), {"scope": "fleet"}, format="json"
    )
    assert resp.status_code == 400
    assert "release" in resp.json()


def test_put_full_update(api_topology, bearer, fleet_target, rel2, group, reconciled):
    resp = bearer(api_topology["staff"]).put(
        reverse(DETAIL, args=[fleet_target.pk]),
        {"release": rel2.pk, "scope": "group", "group": group.pk},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    fleet_target.refresh_from_db()
    assert (fleet_target.version, fleet_target.scope, fleet_target.group) == (
        "2.0",
        "group",
        group,
    )


# --- delete -----------------------------------------------------------------


def test_delete_fleet_target_audits(api_topology, bearer, fleet_target, reconciled):
    resp = bearer(api_topology["staff"]).delete(reverse(DETAIL, args=[fleet_target.pk]))
    assert resp.status_code == 204
    (log,) = _audits()
    assert log.details["action"] == "deleted"
    assert log.details["version"] == "1.0"
    assert "via API token" in log.message
    assert reconciled == ["fm"]
    assert not StationAuditLog.objects.filter(event_type__startswith="firmware_target").exists()


def test_delete_station_target_writes_removed_audit(api_topology, bearer, fm, rel1, reconciled):
    station = api_topology["station_in"]
    target = ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1.0", scope="station", station=station
    )
    resp = bearer(api_topology["staff"]).delete(reverse(DETAIL, args=[target.pk]))
    assert resp.status_code == 204
    (slog,) = _station_audits(StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED)
    assert slog.station == station
    assert "via API token" in slog.message


def test_create_scope_defaults_to_fleet(api_topology, bearer, rel1, reconciled):
    resp = bearer(api_topology["staff"]).post(reverse(LIST), {"release": rel1.pk}, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["scope"] == "fleet"


def test_create_unique_race_returns_409_not_500(
    api_topology, bearer, rel1, reconciled, monkeypatch
):
    """Two concurrent fleet-target creates both pass the form's unique check;
    the loser's IntegrityError must be a 409 (no audit, no reconcile)."""
    from django.db import IntegrityError

    from apps.module_firmware.forms import ModuleFirmwareTargetForm

    def _raise(self, *a, **k):
        raise IntegrityError("duplicate key value violates unique constraint")

    monkeypatch.setattr(ModuleFirmwareTargetForm, "save", _raise)
    resp = bearer(api_topology["staff"]).post(
        reverse(LIST), {"release": rel1.pk, "scope": "fleet"}, format="json"
    )
    assert resp.status_code == 409, resp.content
    assert not ModuleFirmwareTarget.objects.exists()
    assert not _audits().exists()
    assert reconciled == []
