"""Write surface tests for the ImageRelease special endpoint.

Covers: archive/restore (soft-delete lifecycle), available/ (GH proxy),
import/ (queue an ImageImportJob), hard-DELETE blocked (405), permission
matrix (staff pass, region_mgr/station_user/applicant/anon fail).
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Brief verbatim tests (Task 11 required)
# ---------------------------------------------------------------------------


def test_archive_restore_idempotent_staff(api_topology, bearer, image_release):
    t = api_topology
    arch = reverse("api:image-archive", args=[image_release.pk])
    assert bearer(t["staff"]).post(arch).status_code in (200, 204)
    assert bearer(t["staff"]).post(arch).status_code in (200, 204)  # idempotent
    image_release.refresh_from_db()
    assert image_release.archived_at is not None
    rest = reverse("api:image-restore", args=[image_release.pk])
    assert bearer(t["staff"]).post(rest).status_code in (200, 204)
    image_release.refresh_from_db()
    assert image_release.archived_at is None


def test_non_staff_cannot_archive(api_topology, bearer, image_release):
    t = api_topology
    assert (
        bearer(t["region_mgr"])
        .post(reverse("api:image-archive", args=[image_release.pk]))
        .status_code
        == 403
    )


def test_no_hard_delete(api_topology, bearer, image_release):
    t = api_topology
    assert (
        bearer(t["admin"]).delete(reverse("api:image-detail", args=[image_release.pk])).status_code
        == 405
    )


def test_import_rejects_unknown_tag(api_topology, bearer, monkeypatch):
    from apps.images import github_releases

    t = api_topology
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [])
    r = bearer(t["staff"]).post(
        reverse("api:image-import"),
        {"tag": "v-nope", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Extended permission matrix
# ---------------------------------------------------------------------------


def test_station_user_cannot_archive(api_topology, bearer, image_release):
    t = api_topology
    assert (
        bearer(t["station_user"])
        .post(reverse("api:image-archive", args=[image_release.pk]))
        .status_code
        == 403
    )


def test_applicant_cannot_archive(api_topology, bearer, image_release):
    t = api_topology
    assert (
        bearer(t["applicant"])
        .post(reverse("api:image-archive", args=[image_release.pk]))
        .status_code
        == 403
    )


def test_anon_cannot_archive(anon_client, image_release):
    r = anon_client.post(reverse("api:image-archive", args=[image_release.pk]))
    assert r.status_code == 401


def test_non_staff_cannot_restore(api_topology, bearer, image_release):
    t = api_topology
    assert (
        bearer(t["region_mgr"])
        .post(reverse("api:image-restore", args=[image_release.pk]))
        .status_code
        == 403
    )


def test_non_staff_cannot_import(api_topology, bearer):
    """region_mgr → 403 from the can_manage_images gate, and no job row created.

    No monkeypatch of fetch_releases: the gate fires at the very top of the
    action, before any fetch or ImageImportJob creation, so a non-staff POST
    must never reach the network or the DB.
    """
    from apps.images.models import ImageImportJob

    t = api_topology
    before = ImageImportJob.objects.count()
    r = bearer(t["region_mgr"]).post(
        reverse("api:image-import"),
        {"tag": "v2.0", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    assert r.status_code == 403
    assert ImageImportJob.objects.count() == before


def test_anon_cannot_import(anon_client):
    from apps.images.models import ImageImportJob

    before = ImageImportJob.objects.count()
    r = anon_client.post(
        reverse("api:image-import"),
        {"tag": "v2.0", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    assert r.status_code == 401
    assert ImageImportJob.objects.count() == before


# ---------------------------------------------------------------------------
# available/ action
# ---------------------------------------------------------------------------


def _make_fake_release(tag):
    """Return a minimal GitHubRelease-like object for monkeypatching."""
    from apps.images.github_releases import GitHubRelease

    return GitHubRelease(
        tag=tag,
        html_url=f"https://github.com/OE5XRX/linux-image/releases/tag/{tag}",
        is_latest=(tag == "v2.0"),
        asset_names=frozenset(),
    )


def test_available_returns_list_for_staff(api_topology, bearer, monkeypatch):
    from apps.images import github_releases

    fake_releases = [_make_fake_release("v2.0"), _make_fake_release("v1.9")]
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

    t = api_topology
    r = bearer(t["staff"]).get(reverse("api:image-available"))
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    tags = {item["tag"] for item in data}
    assert "v2.0" in tags
    assert "v1.9" in tags


def test_available_forbidden_for_region_mgr(api_topology, bearer, monkeypatch):
    from apps.images import github_releases

    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [])

    t = api_topology
    r = bearer(t["region_mgr"]).get(reverse("api:image-available"))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# import/ action — happy path + audit
# ---------------------------------------------------------------------------


def test_import_happy_path(api_topology, bearer, monkeypatch):
    from apps.accounts.models import AccountAuditLog
    from apps.images import github_releases
    from apps.images.models import ImageImportJob

    fake_releases = [_make_fake_release("v2.0")]
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

    t = api_topology
    r = bearer(t["staff"]).post(
        reverse("api:image-import"),
        {"tag": "v2.0", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    assert r.status_code == 202

    # Job created with correct requested_by
    job = ImageImportJob.objects.get(tag="v2.0", machine="qemux86-64", channel="stable")
    assert job.requested_by == t["staff"]

    # CONFIG_CHANGED audit row with "via API token"
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED
    ).last()
    assert log is not None
    assert "via API token" in log.message
