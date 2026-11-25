"""apps.sstv models: defaults, dedupe constraint, stored-file cleanup, permission area."""

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction

from apps.accounts.permissions import AREAS, Level, can, required_level
from apps.sstv.models import (
    MODE_CHOICES,
    MODE_MAX_LENGTH,
    ActiveSstvRx,
    SstvImage,
    SstvSettings,
    SstvTransmission,
)
from apps.stations.models import Station
from station_agent.sstv.modes import MODES

pytestmark = pytest.mark.django_db


def _image(station, **kw):
    kw.setdefault("direction", SstvImage.Direction.RX)
    kw.setdefault("mode", "martin1")
    img = SstvImage(station=station, **kw)
    img.image.save("p.png", ContentFile(b"png"), save=False)
    img.save()
    return img


def _transmission(station, *, audio=True, **kw):
    tx = SstvTransmission(station=station, slot=1, module="fm", mode="robot36", **kw)
    tx.preview.save("pre.png", ContentFile(b"png"), save=False)
    if audio:
        tx.audio.save("a.raw", ContentFile(b"\0\0"), save=False)
    tx.save()
    return tx


def test_settings_defaults_and_singleton():
    s = SstvSettings.load()
    assert s.pk == 1 and s.retention_days == 90
    s.retention_days = 7
    s.save()
    assert SstvSettings.load().retention_days == 7
    assert SstvSettings.objects.count() == 1


def test_settings_retention_minimum_is_one():
    s = SstvSettings.load()
    s.retention_days = 0
    with pytest.raises(ValidationError):
        s.full_clean()
    s.retention_days = 1
    s.full_clean()


def test_mode_choices_cover_the_mode_table():
    assert [k for k, _ in MODE_CHOICES] == list(MODES)
    assert max(len(k) for k in MODES) <= MODE_MAX_LENGTH


def test_image_defaults(station):
    img = _image(station)
    assert img.complete is True
    assert img.callsign == "" and img.picture_id == "" and img.user is None
    assert img.image.name.startswith("sstv/")
    assert img.created_at is not None


def test_picture_id_unique_per_station(station):
    _image(station, picture_id="abc")
    with pytest.raises(IntegrityError), transaction.atomic():
        _image(station, picture_id="abc")


def test_picture_id_may_repeat_across_stations(station):
    other = Station.objects.create(name="Other", callsign="OE5ABC")
    _image(station, picture_id="abc")
    _image(other, picture_id="abc")
    assert SstvImage.objects.filter(picture_id="abc").count() == 2


def test_blank_picture_id_is_not_unique(station):
    # TX pictures carry no picture id: any number of them per station.
    _image(station, direction=SstvImage.Direction.TX)
    _image(station, direction=SstvImage.Direction.TX)
    assert SstvImage.objects.filter(station=station, picture_id="").count() == 2


def test_transmission_defaults(station):
    tx = _transmission(station, audio=False)
    assert tx.state == SstvTransmission.State.DRAFT
    assert not tx.audio and tx.sha256 == "" and tx.size == 0 and tx.duration_s == 0.0
    assert tx.preview.name.startswith("sstv/preview/")


def test_active_rx_one_per_station(station, member_user):
    rx = ActiveSstvRx.objects.create(station=station, slot=1, user=member_user)
    assert rx.state == ActiveSstvRx.State.STARTING and rx.pk == station.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        ActiveSstvRx.objects.create(station=station, slot=2)


def test_user_delete_keeps_rows(station, member_user):
    img = _image(station, direction="tx", user=member_user, callsign="member")
    tx = _transmission(station, user=member_user)
    rx = ActiveSstvRx.objects.create(station=station, slot=1, user=member_user)
    member_user.delete()
    img.refresh_from_db()
    tx.refresh_from_db()
    rx.refresh_from_db()
    assert img.user is None and img.callsign == "member"
    assert tx.user is None and rx.user is None


def test_image_delete_removes_file(station, django_capture_on_commit_callbacks):
    img = _image(station)
    name = img.image.name
    assert default_storage.exists(name)
    with django_capture_on_commit_callbacks(execute=True):
        img.delete()
    assert not default_storage.exists(name)


def test_queryset_delete_removes_files(station, django_capture_on_commit_callbacks):
    names = [_image(station).image.name for _ in range(3)]
    with django_capture_on_commit_callbacks(execute=True):
        SstvImage.objects.filter(station=station).delete()
    assert not any(default_storage.exists(n) for n in names)


def test_transmission_delete_removes_preview_and_audio(
    station, django_capture_on_commit_callbacks
):
    tx = _transmission(station)
    names = [tx.preview.name, tx.audio.name]
    assert all(default_storage.exists(n) for n in names)
    with django_capture_on_commit_callbacks(execute=True):
        tx.delete()
    assert not any(default_storage.exists(n) for n in names)


def test_transmission_without_audio_deletes_cleanly(station, django_capture_on_commit_callbacks):
    tx = _transmission(station, audio=False)
    name = tx.preview.name
    with django_capture_on_commit_callbacks(execute=True):
        tx.delete()
    assert not default_storage.exists(name)


def test_station_cascade_removes_files(station, django_capture_on_commit_callbacks):
    tx = _transmission(station)
    names = [_image(station).image.name, tx.preview.name, tx.audio.name]
    with django_capture_on_commit_callbacks(execute=True):
        station.delete()
    assert not SstvImage.objects.exists() and not SstvTransmission.objects.exists()
    assert not any(default_storage.exists(n) for n in names)


def test_rolled_back_delete_keeps_files(station, django_capture_on_commit_callbacks):
    img = _image(station)
    name, pk = img.image.name, img.pk
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        try:
            with transaction.atomic():
                img.delete()
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
    # A rolled-back savepoint drops its on_commit callbacks: nothing to run.
    assert callbacks == []
    assert default_storage.exists(name)
    assert SstvImage.objects.filter(pk=pk).exists()


def test_storage_failure_never_breaks_delete(
    station, django_capture_on_commit_callbacks, monkeypatch, caplog
):
    img = _image(station)
    pk = img.pk

    def boom(name):
        raise OSError("s3 down")

    monkeypatch.setattr(img.image.storage, "delete", boom)
    with django_capture_on_commit_callbacks(execute=True):
        img.delete()
    assert not SstvImage.objects.filter(pk=pk).exists()
    assert "could not delete stored file" in caplog.text


# --- permission area -------------------------------------------------------


def test_sstv_area_levels():
    area = AREAS["sstv"]
    assert (area.view, area.use, area.manage) == (Level.APPLICANT, Level.MEMBER, Level.STAFF)
    assert set(area.verb_labels) == {"use", "manage"}


@pytest.mark.parametrize(
    ("fixture", "view", "use", "manage"),
    [
        ("applicant_user", True, False, False),
        ("member_user", True, True, False),
        ("staff_user", True, True, True),
        ("admin_user", True, True, True),
    ],
)
def test_sstv_matrix_per_level(request, fixture, view, use, manage):
    user = request.getfixturevalue(fixture)
    assert can(user, "sstv", "view") is view
    assert can(user, "sstv", "use") is use
    assert can(user, "sstv", "manage") is manage


def test_sstv_required_levels():
    assert required_level("sstv", "view") == Level.APPLICANT
    assert required_level("sstv", "use") == Level.MEMBER
    assert required_level("sstv", "manage") == Level.STAFF
