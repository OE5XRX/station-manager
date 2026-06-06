"""Soft-delete (archive/restore) tests for ImageRelease.

Mirrors the AppGrant.revoked_at soft-delete pattern in apps/sso/models.py.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.images.models import ImageRelease


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
