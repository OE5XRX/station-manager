"""Soft-delete (archive/restore) tests for ImageRelease.

Mirrors the AppGrant.revoked_at soft-delete pattern in apps/sso/models.py.
"""

import pytest
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
