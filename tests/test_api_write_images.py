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

    # Use releases WITH complete importable asset triples so the C4 filter
    # doesn't exclude them (the original _make_fake_release used empty assets,
    # which by design are now filtered out as non-importable).
    fake_releases = [
        _make_release_with_assets("v2.0", machines=["qemux86-64"]),
        _make_release_with_assets("v1.9", machines=["qemux86-64"]),
    ]
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

    # Use a release WITH complete asset triples so the (tag, machine, channel)
    # validation (item 10) passes. channel="stable" is not a standard name, so
    # we build assets explicitly for that channel.
    base = "oe5xrx-qemux86-64-stable-v2.0.wic.bz2"
    from apps.images.github_releases import GitHubRelease

    fake_release = GitHubRelease(
        tag="v2.0",
        html_url="https://github.com/OE5XRX/linux-image/releases/tag/v2.0",
        is_latest=True,
        asset_names=frozenset({base, f"{base}.bundle", f"{base}.sha256"}),
    )
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [fake_release])

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


# ---------------------------------------------------------------------------
# C4 — available/ returns only importable releases
# ---------------------------------------------------------------------------


def _make_release_with_assets(tag, machines=None, channel="release"):
    """Return a GitHubRelease with complete asset triples for given machines."""
    from apps.images.github_releases import GitHubRelease

    machines = machines or []
    asset_names = set()
    for m in machines:
        base = f"oe5xrx-{m}-{channel}-{tag}.wic.bz2"
        asset_names.update({base, f"{base}.bundle", f"{base}.sha256"})
    return GitHubRelease(
        tag=tag,
        html_url=f"https://github.com/OE5XRX/linux-image/releases/tag/{tag}",
        is_latest=(tag == "v2.0"),
        asset_names=frozenset(asset_names),
    )


def _make_incomplete_release(tag):
    """Return a GitHubRelease with NO complete importable asset triples."""
    from apps.images.github_releases import GitHubRelease

    # Only one of the three required files present — not complete
    asset_names = frozenset({f"oe5xrx-qemux86-64-release-{tag}.wic.bz2"})
    return GitHubRelease(
        tag=tag,
        html_url=f"https://github.com/OE5XRX/linux-image/releases/tag/{tag}",
        is_latest=False,
        asset_names=asset_names,
    )


def test_available_returns_only_importable_releases(api_topology, bearer, monkeypatch):
    """available/ must only return releases that have at least one importable
    machine/channel combination (complete asset triple).

    A release with no importable assets must be excluded from the response.
    """
    from apps.images import github_releases

    complete = _make_release_with_assets("v2.0", machines=["qemux86-64"])
    incomplete = _make_incomplete_release("v1.9")
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [complete, incomplete])

    t = api_topology
    r = bearer(t["staff"]).get(reverse("api:image-available"))
    assert r.status_code == 200
    data = r.json()
    tags = [item["tag"] for item in data]
    assert "v2.0" in tags, "Complete release must appear"
    assert "v1.9" not in tags, "Incomplete release must be excluded"


def test_available_no_releases_returns_empty(api_topology, bearer, monkeypatch):
    """If all releases have no importable assets, available/ returns []."""
    from apps.images import github_releases

    incomplete = _make_incomplete_release("v1.0")
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [incomplete])

    t = api_topology
    r = bearer(t["staff"]).get(reverse("api:image-available"))
    assert r.status_code == 200
    assert r.json() == []


def test_available_exposes_channels_per_release(api_topology, bearer, monkeypatch):
    """available/ must expose channels/machines per release so automation can
    feed available/ output straight into import/."""
    from apps.images import github_releases

    # Release with complete assets for two machines
    complete = _make_release_with_assets(
        "v3.0", machines=["qemux86-64", "raspberrypi4-64"], channel="release"
    )
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [complete])

    t = api_topology
    r = bearer(t["staff"]).get(reverse("api:image-available"))
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    entry = data[0]
    assert entry["tag"] == "v3.0"
    # Must expose importable variants
    assert "importable_variants" in entry
    variants = entry["importable_variants"]
    machines_exposed = {v["machine"] for v in variants}
    assert "qemux86-64" in machines_exposed
    assert "raspberrypi4-64" in machines_exposed


# ---------------------------------------------------------------------------
# C5 — import/ duplicate-job guard
# ---------------------------------------------------------------------------


def _make_fake_release_with_stable(tag):
    """Return a GitHubRelease with complete assets for qemux86-64/stable."""
    from apps.images.github_releases import GitHubRelease

    base = f"oe5xrx-qemux86-64-stable-{tag}.wic.bz2"
    return GitHubRelease(
        tag=tag,
        html_url=f"https://github.com/OE5XRX/linux-image/releases/tag/{tag}",
        is_latest=True,
        asset_names=frozenset({base, f"{base}.bundle", f"{base}.sha256"}),
    )


def test_import_duplicate_pending_job_returns_conflict(api_topology, bearer, monkeypatch):
    """import/ for a tag/machine/channel that already has a PENDING job → 409,
    no second job created."""
    from apps.images import github_releases
    from apps.images.models import ImageImportJob

    # Use a release WITH proper assets so the (machine, channel) variant
    # validation (item 10 fix) passes before reaching the duplicate guard.
    fake_releases = [_make_fake_release_with_stable("v2.0")]
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

    t = api_topology
    # Pre-create an active PENDING job
    ImageImportJob.objects.create(
        tag="v2.0",
        machine="qemux86-64",
        channel="stable",
        mark_as_latest=False,
        requested_by=t["staff"],
        status=ImageImportJob.Status.PENDING,
    )
    before = ImageImportJob.objects.filter(
        tag="v2.0", machine="qemux86-64", channel="stable"
    ).count()

    r = bearer(t["staff"]).post(
        reverse("api:image-import"),
        {"tag": "v2.0", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    # Must return conflict (409) or at minimum not 202 when already queued
    assert r.status_code == 409
    assert (
        ImageImportJob.objects.filter(tag="v2.0", machine="qemux86-64", channel="stable").count()
        == before
    ), "No second job should be created when one is already active"


def test_import_duplicate_running_job_returns_conflict(api_topology, bearer, monkeypatch):
    """import/ for a tag/machine/channel that already has a RUNNING job → 409."""
    from apps.images import github_releases
    from apps.images.models import ImageImportJob

    fake_releases = [_make_fake_release_with_stable("v2.0")]
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

    t = api_topology
    ImageImportJob.objects.create(
        tag="v2.0",
        machine="qemux86-64",
        channel="stable",
        mark_as_latest=False,
        requested_by=t["staff"],
        status=ImageImportJob.Status.RUNNING,
    )
    r = bearer(t["staff"]).post(
        reverse("api:image-import"),
        {"tag": "v2.0", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    assert r.status_code == 409


def test_import_fresh_variant_still_202(api_topology, bearer, monkeypatch):
    """import/ for a tag/machine/channel with no active job → 202 as before.

    Uses a release WITH complete asset triples so the (tag, machine, channel)
    variant validation (item 10 fix) passes.
    """
    from apps.images import github_releases

    # Build a release with assets for qemux86-64/stable so channels_for passes.
    base = "oe5xrx-qemux86-64-stable-v2.0.wic.bz2"
    from apps.images.github_releases import GitHubRelease

    fake_release = GitHubRelease(
        tag="v2.0",
        html_url="https://github.com/OE5XRX/linux-image/releases/tag/v2.0",
        is_latest=True,
        asset_names=frozenset({base, f"{base}.bundle", f"{base}.sha256"}),
    )
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [fake_release])

    t = api_topology
    r = bearer(t["staff"]).post(
        reverse("api:image-import"),
        {"tag": "v2.0", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    assert r.status_code == 202


def test_import_already_imported_returns_conflict(api_topology, bearer, monkeypatch):
    """import/ for a tag/machine/channel that is already imported → 409."""
    from apps.images import github_releases
    from apps.images.models import ImageRelease

    # Use a release WITH proper assets so the (machine, channel) variant
    # validation (item 10 fix) passes before reaching the already-imported guard.
    fake_releases = [_make_fake_release_with_stable("v2.0")]
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

    t = api_topology
    # Create the already-imported release
    ImageRelease.objects.create(
        tag="v2.0",
        machine="qemux86-64",
        channel="stable",
        s3_key="images/v2.0/qemux86-64.wic.bz2",
        sha256="a" * 64,
        size_bytes=1000,
    )
    r = bearer(t["staff"]).post(
        reverse("api:image-import"),
        {"tag": "v2.0", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    assert r.status_code == 409
