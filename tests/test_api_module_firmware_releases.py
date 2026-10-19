"""Write API (/api/v1/module-firmware-releases/) — archive/restore, the GitHub
``available`` proxy and ``import`` (all ``modules.manage``)."""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog
from apps.api.models import PersonalAccessToken
from apps.images.github_releases import GitHubAPIError, GitHubRelease
from apps.module_firmware import ops
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleType,
)

pytestmark = pytest.mark.django_db

GH_FETCH = "apps.images.github_releases.fetch_releases"
ARCHIVE = "api:module-firmware-release-archive"
RESTORE = "api:module-firmware-release-restore"
DETAIL = "api:module-firmware-release-detail"
AVAILABLE = "api:module-firmware-release-available"
IMPORT = "api:module-firmware-release-import"
TAG = "26.07.04-01"


@pytest.fixture
def fm():
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )


@pytest.fixture
def release(fm):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="1.0",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="OE5XRX/FW-RemoteStation",
        source_tag="v1.0",
    )


@pytest.fixture
def reconciled(monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "reconcile_module_type", lambda mt: calls.append(mt.key))
    return calls


@pytest.fixture
def github(monkeypatch):
    """Set the fake GitHub releases list; a GitHubAPIError instance raises."""
    state = {"value": []}

    def _fetch(repo, *args, **kwargs):
        if isinstance(state["value"], Exception):
            raise state["value"]
        return state["value"]

    monkeypatch.setattr(GH_FETCH, _fetch)

    def _set(value):
        state["value"] = value

    return _set


def _gh_release(tag, *signed, is_latest=False):
    names = set()
    for s in signed:
        names.add(s)
        names.add(s + ".bundle")
    return GitHubRelease(
        tag=tag,
        html_url=f"https://github.com/OE5XRX/FW-RemoteStation/releases/tag/{tag}",
        is_latest=is_latest,
        asset_names=frozenset(names),
    )


def _audits():
    return AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)


def _suffix(user):
    return f"via API token {PersonalAccessToken.objects.get(user=user).prefix}"


# ------------------------------------------------------------ archive/restore


def test_archive_and_restore_idempotent_with_one_audit_each(
    api_topology, bearer, release, reconciled
):
    staff = api_topology["staff"]
    client = bearer(staff)

    for _ in range(2):
        resp = client.post(reverse(ARCHIVE, args=[release.pk]))
        assert resp.status_code == 200, resp.content
        assert resp.json()["id"] == release.pk
        assert resp.json()["archived_at"] is not None
    release.refresh_from_db()
    assert release.archived_at is not None
    (log,) = _audits()
    assert log.details["action"] == "archived"
    assert log.message.endswith(_suffix(staff))
    assert reconciled == ["fm"]

    for _ in range(2):
        resp = client.post(reverse(RESTORE, args=[release.pk]))
        assert resp.status_code == 200, resp.content
        assert resp.json()["archived_at"] is None
    release.refresh_from_db()
    assert release.archived_at is None
    assert _audits().count() == 2
    restored = _audits().order_by("-pk").first()
    assert restored.details["action"] == "restored"
    assert restored.message.endswith(_suffix(staff))
    assert reconciled == ["fm", "fm"]


def test_archive_unknown_id_404(api_topology, bearer):
    resp = bearer(api_topology["staff"]).post(reverse(ARCHIVE, args=[999999]))
    assert resp.status_code == 404


def test_member_archive_forbidden(api_topology, bearer, release):
    resp = bearer(api_topology["station_user"]).post(reverse(ARCHIVE, args=[release.pk]))
    assert resp.status_code == 403
    assert resp.json()["required"] == "modules.manage"
    release.refresh_from_db()
    assert release.archived_at is None
    assert not _audits().exists()


def test_delete_release_not_allowed(api_topology, bearer, release):
    resp = bearer(api_topology["staff"]).delete(reverse(DETAIL, args=[release.pk]))
    assert resp.status_code == 405
    assert ModuleFirmwareRelease.all_objects.filter(pk=release.pk).exists()


# ------------------------------------------------------------------ available


def test_available_lists_rows_with_missing_variants(api_topology, bearer, fm, github):
    Module.objects.create(uid="U1", module_type=fm, variant="vhf")
    Module.objects.create(uid="U2", module_type=fm, variant="uhf")
    github(
        [
            _gh_release(TAG, "fm-sa818-vhf.signed.bin", is_latest=True),
            _gh_release("26.07.01-01", "other-vhf.signed.bin"),
        ]
    )
    resp = bearer(api_topology["staff"]).get(reverse(AVAILABLE), {"module_type": fm.pk})
    assert resp.status_code == 200, resp.content
    assert resp.json() == [
        {
            "tag": TAG,
            "html_url": f"https://github.com/OE5XRX/FW-RemoteStation/releases/tag/{TAG}",
            "is_latest": True,
            "state": "ready",
            "variants": ["vhf"],
            "missing_variants": ["uhf"],
        },
        {
            "tag": "26.07.01-01",
            "html_url": ("https://github.com/OE5XRX/FW-RemoteStation/releases/tag/26.07.01-01"),
            "is_latest": False,
            "state": "no_assets",
            "variants": [],
            "missing_variants": ["uhf", "vhf"],
        },
    ]


@pytest.mark.parametrize("query", [{}, {"module_type": ""}, {"module_type": "abc"}])
def test_available_requires_int_module_type(api_topology, bearer, query):
    resp = bearer(api_topology["staff"]).get(reverse(AVAILABLE), query)
    assert resp.status_code == 400
    assert "module_type" in resp.json()


def test_available_unknown_type_404(api_topology, bearer):
    resp = bearer(api_topology["staff"]).get(reverse(AVAILABLE), {"module_type": 999999})
    assert resp.status_code == 404


def test_available_blocked_type_400(api_topology, bearer, github):
    untrusted = ModuleType.objects.create(
        key="x", display_name="X", firmware_repo="evil/fw", release_asset_prefix="x"
    )
    github(GitHubAPIError("must not be called"))
    resp = bearer(api_topology["staff"]).get(reverse(AVAILABLE), {"module_type": untrusted.pk})
    assert resp.status_code == 400
    assert resp.json()["module_type"]


def test_available_github_error_502(api_topology, bearer, fm, github):
    github(GitHubAPIError("rate limited"))
    resp = bearer(api_topology["staff"]).get(reverse(AVAILABLE), {"module_type": fm.pk})
    assert resp.status_code == 502


def test_member_available_forbidden(api_topology, bearer, fm, github):
    resp = bearer(api_topology["station_user"]).get(reverse(AVAILABLE), {"module_type": fm.pk})
    assert resp.status_code == 403
    assert resp.json()["required"] == "modules.manage"


# --------------------------------------------------------------------- import


def test_import_ready_queues_job_then_conflict(api_topology, bearer, fm, github):
    github([_gh_release(TAG, "fm-sa818-vhf.signed.bin")])
    staff = api_topology["staff"]
    client = bearer(staff)
    body = {"module_type": fm.pk, "tag": TAG}

    resp = client.post(reverse(IMPORT), body, format="json")
    assert resp.status_code == 202, resp.content
    job = ModuleFirmwareImportJob.objects.get()
    data = resp.json()
    assert data["id"] == job.pk
    assert data["tag"] == TAG
    assert data["module_type"] == fm.pk
    assert data["status"] == "pending"
    assert data["requested_by"] == staff.username
    (log,) = _audits()
    assert log.details["action"] == "queued"
    assert log.message.endswith(_suffix(staff))

    # Same tag again: the release row is now "queued" → 409, still one job.
    resp = client.post(reverse(IMPORT), body, format="json")
    assert resp.status_code == 409
    assert ModuleFirmwareImportJob.objects.count() == 1
    assert _audits().count() == 1


def test_import_already_imported_tag_409(api_topology, bearer, fm, release, github):
    github([_gh_release("v1.0", "fm-sa818-vhf.signed.bin")])
    resp = bearer(api_topology["staff"]).post(
        reverse(IMPORT), {"module_type": fm.pk, "tag": "v1.0"}, format="json"
    )
    assert resp.status_code == 409
    assert not ModuleFirmwareImportJob.objects.exists()


def test_import_unknown_tag_400(api_topology, bearer, fm, github):
    github([_gh_release(TAG, "fm-sa818-vhf.signed.bin")])
    resp = bearer(api_topology["staff"]).post(
        reverse(IMPORT), {"module_type": fm.pk, "tag": "nope"}, format="json"
    )
    assert resp.status_code == 400
    assert "tag" in resp.json()
    assert not ModuleFirmwareImportJob.objects.exists()


def test_import_tag_without_assets_400(api_topology, bearer, fm, github):
    github([_gh_release(TAG, "other-vhf.signed.bin")])
    resp = bearer(api_topology["staff"]).post(
        reverse(IMPORT), {"module_type": fm.pk, "tag": TAG}, format="json"
    )
    assert resp.status_code == 400
    assert "tag" in resp.json()
    assert not ModuleFirmwareImportJob.objects.exists()


def test_import_untrusted_type_400(api_topology, bearer, github):
    untrusted = ModuleType.objects.create(
        key="x", display_name="X", firmware_repo="evil/fw", release_asset_prefix="x"
    )
    github([_gh_release(TAG, "x-vhf.signed.bin")])
    resp = bearer(api_topology["staff"]).post(
        reverse(IMPORT), {"module_type": untrusted.pk, "tag": TAG}, format="json"
    )
    assert resp.status_code == 400
    assert "module_type" in resp.json()
    assert not ModuleFirmwareImportJob.objects.exists()


@pytest.mark.parametrize("module_type", [999999, "abc", None])
def test_import_bad_module_type_400(api_topology, bearer, github, module_type):
    resp = bearer(api_topology["staff"]).post(
        reverse(IMPORT), {"module_type": module_type, "tag": TAG}, format="json"
    )
    assert resp.status_code == 400
    assert "module_type" in resp.json()


def test_import_github_error_502(api_topology, bearer, fm, github):
    github(GitHubAPIError("down"))
    resp = bearer(api_topology["staff"]).post(
        reverse(IMPORT), {"module_type": fm.pk, "tag": TAG}, format="json"
    )
    assert resp.status_code == 502
    assert not ModuleFirmwareImportJob.objects.exists()


def test_member_import_forbidden(api_topology, bearer, fm, github):
    github([_gh_release(TAG, "fm-sa818-vhf.signed.bin")])
    resp = bearer(api_topology["station_user"]).post(
        reverse(IMPORT), {"module_type": fm.pk, "tag": TAG}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["required"] == "modules.manage"
    assert not ModuleFirmwareImportJob.objects.exists()


def test_openapi_release_actions():
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(public=True)
    paths = schema["paths"]
    base = "/api/v1/module-firmware-releases/"
    assert "post" in paths[base + "{id}/archive/"]
    assert "post" in paths[base + "{id}/restore/"]
    available = paths[base + "available/"]["get"]
    (param,) = [p for p in available["parameters"] if p["name"] == "module_type"]
    assert param["required"] is True
    item = schema["components"]["schemas"]["ModuleFirmwareGitHubRelease"]["properties"]
    assert set(item) == {"tag", "html_url", "is_latest", "state", "variants", "missing_variants"}
    imp = paths[base + "import/"]["post"]
    assert "202" in imp["responses"]
    ref = imp["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body = schema["components"]["schemas"][ref.rsplit("/", 1)[1]]["properties"]
    assert set(body) == {"module_type", "tag"}
