"""Retention steps for SSTV: gallery pictures after SstvSettings.retention_days,
transmission leftovers (drafts, aborted, error …) after 1 h — files included."""

from datetime import timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.accounts.models import User
from apps.audit import retention
from apps.audit.retention import RetentionConfig, run_retention
from apps.sstv.models import SstvImage, SstvSettings, SstvTransmission

pytestmark = pytest.mark.django_db

NOW = timezone.now()
S = SstvTransmission.State


def _cfg(enforce=True, batch_size=1000):
    return RetentionConfig(
        now=NOW, audit_days=365, ip_days=90, enforce=enforce, batch_size=batch_size
    )


def _image(station, age, **kw):
    img = SstvImage(station=station, direction="rx", mode="martin1", **kw)
    img.image.save("p.png", ContentFile(b"png"), save=False)
    img.save()
    SstvImage.objects.filter(pk=img.pk).update(created_at=NOW - age)
    return img


def _tx(station, age, state, **kw):
    tx = SstvTransmission(station=station, slot=1, module="fm", mode="robot36", state=state, **kw)
    tx.preview.save("pre.png", ContentFile(b"png"), save=False)
    tx.audio.save("a.raw", ContentFile(b"\0\0"), save=False)
    tx.save()
    SstvTransmission.objects.filter(pk=tx.pk).update(created_at=NOW - age, updated_at=NOW - age)
    return tx


def _exists(obj):
    return type(obj).objects.filter(pk=obj.pk).exists()


def test_pictures_older_than_retention_deleted_with_files(
    station, django_capture_on_commit_callbacks
):
    old = _image(station, timedelta(days=91))
    edge = _image(station, timedelta(days=89))
    names = old.image.name, edge.image.name
    with django_capture_on_commit_callbacks(execute=True):
        result = run_retention(_cfg())
    assert result.errors == []
    assert result.counts["sstv_pictures"] == 1
    assert not _exists(old) and _exists(edge)
    assert not default_storage.exists(names[0]) and default_storage.exists(names[1])


def test_pictures_follow_settings_retention_days(station):
    s = SstvSettings.load()
    s.retention_days = 7
    s.save()
    old = _image(station, timedelta(days=8))
    young = _image(station, timedelta(days=6))
    assert run_retention(_cfg()).counts["sstv_pictures"] == 1
    assert not _exists(old) and _exists(young)


def test_retention_days_below_one_count_as_one(station):
    SstvSettings.load()
    SstvSettings.objects.update(retention_days=0)  # bypasses the form validator
    today = _image(station, timedelta(hours=12))
    old = _image(station, timedelta(days=2))
    assert run_retention(_cfg()).counts["sstv_pictures"] == 1
    assert _exists(today) and not _exists(old)


@pytest.mark.parametrize(
    "state,max_age",
    [
        (S.DRAFT, timedelta(hours=1)),
        (S.ABORTED, timedelta(hours=1)),
        (S.ERROR, timedelta(hours=1)),
        (S.DONE, timedelta(hours=1)),
        (S.SENDING, timedelta(hours=2)),
    ],
)
def test_stale_transmissions_deleted_with_files(
    station, django_capture_on_commit_callbacks, state, max_age
):
    stale = _tx(station, max_age + timedelta(minutes=1), state)
    fresh = _tx(station, max_age - timedelta(minutes=1), state)
    names = stale.preview.name, stale.audio.name
    with django_capture_on_commit_callbacks(execute=True):
        result = run_retention(_cfg())
    assert result.counts["sstv_stale_drafts"] == 1
    assert not _exists(stale) and _exists(fresh)
    assert not any(default_storage.exists(n) for n in names)
    assert default_storage.exists(fresh.preview.name)


def test_sending_row_kept_even_if_updated_at_never_moved(station):
    """Sent from a 59-min-old draft via QuerySet.update() (no auto_now): still on air
    at 61 min — created_at alone decides, with the 2 h margin."""
    tx = _tx(station, timedelta(minutes=61), S.SENDING)  # updated_at == created_at
    assert run_retention(_cfg()).counts["sstv_stale_drafts"] == 0
    assert _exists(tx)


def test_dry_run_counts_only(station, django_capture_on_commit_callbacks):
    img = _image(station, timedelta(days=100))
    tx = _tx(station, timedelta(hours=2), S.DRAFT)
    with django_capture_on_commit_callbacks(execute=True):
        result = run_retention(_cfg(enforce=False))
    assert result.counts["sstv_pictures"] == 1
    assert result.counts["sstv_stale_drafts"] == 1
    assert _exists(img) and _exists(tx)
    assert default_storage.exists(img.image.name)
    assert default_storage.exists(tx.audio.name)


def test_second_run_is_zero_and_batches(station, django_capture_on_commit_callbacks):
    for _ in range(5):
        _image(station, timedelta(days=100))
        _tx(station, timedelta(hours=2), S.ABORTED)
    with django_capture_on_commit_callbacks(execute=True):
        first = run_retention(_cfg(batch_size=2))
    assert first.counts["sstv_pictures"] == 5
    assert first.counts["sstv_stale_drafts"] == 5
    second = run_retention(_cfg())
    assert second.counts["sstv_pictures"] == 0
    assert second.counts["sstv_stale_drafts"] == 0


def test_held_user_pictures_are_kept(monkeypatch, station):
    held = User.objects.create_user(username="held", password="x", membership_level="member")
    monkeypatch.setattr(retention, "held_user_ids", lambda: frozenset({held.pk}))
    kept = _image(station, timedelta(days=100), user=held)
    gone = _image(station, timedelta(days=100))
    assert run_retention(_cfg()).counts["sstv_pictures"] == 1
    assert _exists(kept) and not _exists(gone)


def test_storage_failure_does_not_break_the_step(
    monkeypatch, station, django_capture_on_commit_callbacks
):
    img = _image(station, timedelta(days=100))

    def boom(name):
        raise OSError("s3 down")

    monkeypatch.setattr(img.image.storage, "delete", boom)
    with django_capture_on_commit_callbacks(execute=True):
        result = run_retention(_cfg())
    assert result.errors == []
    assert result.counts["sstv_pictures"] == 1
    assert not _exists(img)
