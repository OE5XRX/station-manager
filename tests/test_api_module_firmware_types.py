"""Write API (/api/v1/module-types/) — create/update/delete (``modules.manage``)."""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog
from apps.api.models import PersonalAccessToken
from apps.module_firmware.models import ModuleFirmwareRelease, ModuleType
from tests.test_api_read_fixtures import session

pytestmark = pytest.mark.django_db

LIST = "api:module-type-list"
DETAIL = "api:module-type-detail"


@pytest.fixture(autouse=True)
def _trusted_owner(settings):
    settings.MODULE_FIRMWARE_TRUSTED_REPO_OWNERS = ["OE5XRX"]


@pytest.fixture
def fm():
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm",
    )


def _payload(**kw):
    data = {
        "key": "uhf",
        "display_name": "UHF",
        "hw_repo": "OE5XRX/HW-Module-UHF",
        "firmware_repo": "OE5XRX/FW-RemoteStation",
        "release_asset_prefix": "uhf",
    }
    data.update(kw)
    return data


def _audits():
    return AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)


def test_create_as_staff_returns_read_shape_and_audits(api_topology, bearer):
    staff = api_topology["staff"]
    client = bearer(staff)
    prefix = PersonalAccessToken.objects.get(user=staff).prefix

    resp = client.post(reverse(LIST), _payload(), format="json")

    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["key"] == "uhf"
    assert body["import_blockers"] == []
    assert body["module_count"] == 0
    assert body["release_count"] == 0
    assert body["target_count"] == 0
    assert ModuleType.objects.filter(key="uhf").exists()
    (log,) = _audits()
    assert log.message.endswith(f"via API token {prefix}")
    assert log.details["object"] == "module_type"
    assert log.details["action"] == "created"
    assert log.details["key"] == "uhf"
    assert log.actor == staff


@pytest.mark.parametrize(
    "role,expected",
    [
        ("station_user", (403,)),
        ("applicant", (403,)),
        ("anon", (401, 403)),
    ],
)
def test_create_forbidden_below_staff(api_topology, bearer, anon_client, role, expected):
    if role == "anon":
        client = anon_client
    elif role == "applicant":
        client = session(api_topology["applicant"])
    else:
        client = bearer(api_topology[role])
    resp = client.post(reverse(LIST), _payload(), format="json")
    assert resp.status_code in expected
    assert not ModuleType.objects.filter(key="uhf").exists()
    assert not _audits().exists()


def test_create_untrusted_firmware_repo_400(api_topology, bearer):
    client = bearer(api_topology["staff"])
    resp = client.post(reverse(LIST), _payload(firmware_repo="evil/FW"), format="json")
    assert resp.status_code == 400
    assert "firmware_repo" in resp.json()
    assert not ModuleType.objects.filter(key="uhf").exists()
    assert not _audits().exists()


def test_create_bad_release_asset_prefix_400(api_topology, bearer):
    client = bearer(api_topology["staff"])
    resp = client.post(reverse(LIST), _payload(release_asset_prefix="uhf-"), format="json")
    assert resp.status_code == 400
    assert "release_asset_prefix" in resp.json()


def test_create_duplicate_key_400(api_topology, bearer, fm):
    client = bearer(api_topology["staff"])
    resp = client.post(reverse(LIST), _payload(key="fm"), format="json")
    assert resp.status_code == 400
    assert "key" in resp.json()


def test_patch_updates_and_audits_changes(api_topology, bearer, fm):
    client = bearer(api_topology["staff"])
    resp = client.patch(reverse(DETAIL, args=[fm.pk]), {"display_name": "FM 2m"}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["display_name"] == "FM 2m"
    assert "import_blockers" in resp.json()
    fm.refresh_from_db()
    assert fm.display_name == "FM 2m"
    (log,) = _audits()
    assert log.details["action"] == "updated"
    assert log.details["changes"] == {"display_name": {"old": "FM", "new": "FM 2m"}}
    assert "via API token" in log.message


def test_patch_key_is_ignored(api_topology, bearer, fm):
    client = bearer(api_topology["staff"])
    resp = client.patch(reverse(DETAIL, args=[fm.pk]), {"key": "other"}, format="json")
    assert resp.status_code == 200, resp.content
    fm.refresh_from_db()
    assert fm.key == "fm"
    assert resp.json()["key"] == "fm"


def test_patch_noop_writes_no_audit(api_topology, bearer, fm):
    client = bearer(api_topology["staff"])
    resp = client.patch(reverse(DETAIL, args=[fm.pk]), {"display_name": "FM"}, format="json")
    assert resp.status_code == 200, resp.content
    assert not _audits().exists()


def test_put_untrusted_repo_400(api_topology, bearer, fm):
    client = bearer(api_topology["staff"])
    resp = client.put(
        reverse(DETAIL, args=[fm.pk]), _payload(key="fm", firmware_repo="evil/FW"), format="json"
    )
    assert resp.status_code == 400
    assert "firmware_repo" in resp.json()
    fm.refresh_from_db()
    assert fm.firmware_repo == "OE5XRX/FW-RemoteStation"


def test_patch_forbidden_for_member(api_topology, bearer, fm):
    client = bearer(api_topology["station_user"])
    resp = client.patch(reverse(DETAIL, args=[fm.pk]), {"display_name": "X"}, format="json")
    assert resp.status_code == 403


def test_delete_unreferenced_204_and_audit(api_topology, bearer, fm):
    client = bearer(api_topology["staff"])
    resp = client.delete(reverse(DETAIL, args=[fm.pk]))
    assert resp.status_code == 204
    assert not ModuleType.objects.filter(pk=fm.pk).exists()
    (log,) = _audits()
    assert log.details["action"] == "deleted"
    assert log.details["key"] == "fm"
    assert "via API token" in log.message


def test_delete_with_archived_release_409(api_topology, bearer, fm):
    from django.utils import timezone

    ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        cosign_bundle_key="k.bundle",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="OE5XRX/FW-RemoteStation",
        source_tag="v26.09.15-01",
        archived_at=timezone.now(),
    )
    client = bearer(api_topology["staff"])
    resp = client.delete(reverse(DETAIL, args=[fm.pk]))
    assert resp.status_code == 409
    assert "detail" in resp.json()
    assert ModuleType.objects.filter(pk=fm.pk).exists()
    assert not _audits().exists()
