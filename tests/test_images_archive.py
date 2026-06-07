"""Soft-delete (archive/restore) tests for ImageRelease.

Mirrors the AppGrant.revoked_at soft-delete pattern in apps/sso/models.py.
"""

from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.images.models import ImageImportJob, ImageRelease


def _make_release(tag="v1-alpha", machine="qemux86-64", is_latest=False, archived=False):
    rel = ImageRelease.objects.create(
        tag=tag,
        machine=machine,
        s3_key=f"images/{tag}/{machine}.wic.bz2",
        sha256="a" * 64,
        size_bytes=100,
        is_latest=is_latest,
    )
    if archived:
        rel.archived_at = timezone.now()
        rel.is_latest = False  # see archive() — never combine archived + latest
        rel.save(update_fields=["archived_at", "is_latest"])
    return rel


@pytest.mark.django_db
def test_default_manager_hides_archived():
    active = _make_release(tag="v1-active")
    _make_release(tag="v1-old", archived=True)

    pks = list(ImageRelease.objects.values_list("pk", flat=True))
    assert pks == [active.pk]


@pytest.mark.django_db
def test_all_objects_manager_returns_everything():
    active = _make_release(tag="v1-active")
    archived = _make_release(tag="v1-old", archived=True)

    pks = set(ImageRelease.all_objects.values_list("pk", flat=True))
    assert pks == {active.pk, archived.pk}


@pytest.mark.django_db
def test_archive_sets_archived_at_and_clears_is_latest():
    rel = _make_release(tag="v1-current", is_latest=True)

    rel.archive()
    rel.refresh_from_db()

    assert rel.archived_at is not None
    assert rel.is_latest is False


@pytest.mark.django_db
def test_archive_is_idempotent():
    rel = _make_release(tag="v1-current", archived=True)
    first_ts = rel.archived_at

    rel.archive()
    rel.refresh_from_db()

    # archive() on an already-archived row must not bump the timestamp
    # — restoring then re-archiving should be the only path to a new ts.
    assert rel.archived_at == first_ts


@pytest.mark.django_db
def test_restore_clears_archived_at():
    rel = _make_release(tag="v1-old", archived=True)

    rel.restore()
    rel.refresh_from_db()

    assert rel.archived_at is None
    # restore() must NOT silently re-promote to latest — that's a
    # separate operator action.
    assert rel.is_latest is False


@pytest.mark.django_db
def test_restore_is_idempotent_on_active_row():
    rel = _make_release(tag="v1-current")

    rel.restore()
    rel.refresh_from_db()

    assert rel.archived_at is None


@pytest.mark.django_db
def test_delete_view_can_act_on_archived_release(client, admin_user, monkeypatch):
    """Hard-delete via URL must remain reachable for an archived release
    (returning the PROTECT-FK flash, or actually deleting if no
    references) — not silently 404 because the default manager now
    filters archived rows."""
    # Stub S3 so the test doesn't try to talk to any storage backend.
    monkeypatch.setattr("apps.images.storage.delete", lambda key: None)

    rel = _make_release(tag="v1-old", archived=True)

    client.force_login(admin_user)
    resp = client.post(reverse("images:delete", kwargs={"pk": rel.pk}))

    # Either 302 (deleted — no FK references in this test) or 302 with
    # the protected flash. Both go to images:list. The thing that must
    # NOT happen is 404.
    assert resp.status_code == 302


@pytest.mark.django_db
def test_mark_latest_view_can_act_on_archived_release(client, admin_user):
    """Mark-latest via URL must remain reachable for an archived release.

    Whether mark-latest on an archived release is semantically meaningful
    is a future product call (today the UI doesn't link it); the URL
    must not silently 404 because of the manager-default change in B2."""
    rel = _make_release(tag="v1-old", archived=True)

    client.force_login(admin_user)
    resp = client.post(reverse("images:mark_latest", kwargs={"pk": rel.pk}))

    assert resp.status_code == 302


@pytest.mark.django_db
def test_archive_view_archives_release(client, admin_user):
    rel = _make_release(tag="v1-current", is_latest=True)

    client.force_login(admin_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": rel.pk}))

    assert resp.status_code == 302
    rel.refresh_from_db()
    assert rel.archived_at is not None
    assert rel.is_latest is False


@pytest.mark.django_db
def test_archive_view_requires_admin(client, member_user):
    rel = _make_release(tag="v1-current")

    client.force_login(member_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": rel.pk}))

    # AdminRequiredMixin raises PermissionDenied for authenticated
    # non-admins → 403.
    assert resp.status_code == 403
    rel.refresh_from_db()
    assert rel.archived_at is None


@pytest.mark.django_db
def test_archive_view_404_on_unknown_pk(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": 99999}))

    assert resp.status_code == 404


@pytest.mark.django_db
def test_archive_view_works_on_release_with_referenced_deployment(
    client, admin_user, station
):
    """The whole point of archive vs hard-delete: archive succeeds
    even when Deployment/ProvisioningJob FKs would PROTECT a delete."""
    from apps.deployments.models import Deployment

    rel = _make_release(tag="v1-shipped")
    Deployment.objects.create(
        image_release=rel,
        target_type=Deployment.TargetType.ALL,
        created_by=admin_user,
    )

    client.force_login(admin_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": rel.pk}))

    assert resp.status_code == 302
    rel.refresh_from_db()
    assert rel.archived_at is not None


@pytest.mark.django_db
def test_restore_view_restores_release(client, admin_user):
    rel = _make_release(tag="v1-old", archived=True)

    client.force_login(admin_user)
    resp = client.post(reverse("images:restore", kwargs={"pk": rel.pk}))

    assert resp.status_code == 302
    rel.refresh_from_db()
    assert rel.archived_at is None
    # restore() must NOT auto-promote to latest
    assert rel.is_latest is False


@pytest.mark.django_db
def test_restore_view_requires_admin(client, member_user):
    rel = _make_release(tag="v1-old", archived=True)

    client.force_login(member_user)
    resp = client.post(reverse("images:restore", kwargs={"pk": rel.pk}))

    assert resp.status_code == 403


@pytest.mark.django_db
def test_restore_view_404_on_unknown_pk(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(reverse("images:restore", kwargs={"pk": 99999}))

    assert resp.status_code == 404


@pytest.fixture
def import_stubs():
    """Stub the heavy paths in _run_import_job so we exercise only
    the DB write — no GitHub fetch, no cosign, no S3, no rootfs
    extraction. Each fake return is just enough for the worker to
    reach update_or_create."""
    fake_asset = type(
        "FakeAsset",
        (),
        {
            "wic_bytes": b"",
            "bundle_bytes": b"",
            "sha256": "b" * 64,
        },
    )()

    def _fake_extract(_decompressed, rootfs_out):
        # The real extract_rootfs writes the rootfs archive to
        # ``rootfs_out`` — the worker then reads it back to hand the
        # bytes to image_storage.upload_bytes. Mirror that side effect
        # so the read_bytes() call after extract doesn't FileNotFound.
        rootfs_out.write_bytes(b"")
        return (0, "c" * 64)

    with (
        patch(
            "apps.provisioning.management.commands.run_background_jobs.github.fetch_release_asset",
            return_value=fake_asset,
        ),
        patch(
            "apps.provisioning.management.commands.run_background_jobs.cosign.verify_blob"
        ),
        patch(
            "apps.provisioning.management.commands.run_background_jobs.image_storage.upload_bytes"
        ),
        patch(
            "apps.provisioning.management.commands.run_background_jobs._decompress_to"
        ),
        patch(
            "apps.provisioning.management.commands.run_background_jobs.extraction.extract_rootfs",
            side_effect=_fake_extract,
        ),
    ):
        yield


@pytest.mark.django_db
def test_image_list_hides_archived_by_default(client, admin_user):
    _make_release(tag="v1-active")
    _make_release(tag="v1-old", archived=True)

    client.force_login(admin_user)
    resp = client.get(reverse("images:list"))

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "v1-active" in body
    assert "v1-old" not in body


@pytest.mark.django_db
def test_image_list_shows_archived_with_query_param(client, admin_user):
    _make_release(tag="v1-active")
    _make_release(tag="v1-old", archived=True)

    client.force_login(admin_user)
    resp = client.get(reverse("images:list") + "?show_archived=1")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "v1-active" in body
    assert "v1-old" in body


@pytest.mark.django_db
def test_reimport_auto_restores_archived_release(import_stubs, admin_user):
    from apps.provisioning.management.commands.run_background_jobs import (
        _run_import_job,
    )

    archived = _make_release(tag="v1-alpha", archived=True)
    archived_pk = archived.pk

    job = ImageImportJob.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        mark_as_latest=False,
        requested_by=admin_user,
    )
    _run_import_job(job)

    # update_or_create returned the SAME row (full-unique on
    # tag/machine guarantees that), with archived_at cleared.
    archived.refresh_from_db()
    assert archived.pk == archived_pk  # not a brand-new row
    assert archived.archived_at is None


@pytest.mark.django_db
def test_archive_then_reimport_workflow(import_stubs, admin_user, client):
    """End-to-end: operator archives a release, later re-imports
    the same tag, observes the row is restored with updated fields
    and surfaces in the default (non-archived) UI list again.

    NOTE on the chosen tag: the ImageImportForm's tag-field help text
    is literally "GitHub release tag, e.g. v1-alpha", which means any
    body-level assertion on "v1-alpha" would collide with form copy
    on the same page. Use a tag that's distinct from that example.
    """
    rel = _make_release(tag="v9-workflow")
    rel_pk = rel.pk

    # Step 1: operator archives via the UI
    client.force_login(admin_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": rel_pk}))
    assert resp.status_code == 302
    rel.refresh_from_db()
    assert rel.archived_at is not None

    # Drain the post-archive success flash ("Release v9-workflow archived.")
    # so it doesn't pollute the next GET's body — the messages framework
    # consumes the queue on first iteration in the template.
    client.get(reverse("images:list"))

    # Default list view no longer shows v9-workflow
    resp = client.get(reverse("images:list"))
    assert b"v9-workflow" not in resp.content

    # Step 2: re-import the same tag from "GitHub"
    from apps.images.models import ImageImportJob
    from apps.provisioning.management.commands.run_background_jobs import (
        _run_import_job,
    )

    job = ImageImportJob.objects.create(
        tag="v9-workflow",
        machine="qemux86-64",
        mark_as_latest=False,
        requested_by=admin_user,
    )
    _run_import_job(job)

    # The very same row (same pk) is back to active
    rel.refresh_from_db()
    assert rel.pk == rel_pk
    assert rel.archived_at is None

    # And it surfaces in the default list view
    resp = client.get(reverse("images:list"))
    assert b"v9-workflow" in resp.content
