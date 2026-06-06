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
