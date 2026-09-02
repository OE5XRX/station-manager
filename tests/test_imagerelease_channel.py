import pytest
from django.db import IntegrityError, transaction

from apps.images.models import ImageRelease


def _mk(**kw):
    defaults = dict(
        tag="v1.0.0",
        machine="qemux86-64",
        s3_key="k",
        sha256="a" * 64,
        size_bytes=1,
    )
    defaults.update(kw)
    return ImageRelease.objects.create(**defaults)


@pytest.mark.django_db
def test_channel_defaults_to_release():
    rel = _mk()
    assert rel.channel == "release"


@pytest.mark.django_db
def test_same_tag_machine_different_channel_allowed():
    _mk(channel="release")
    _mk(channel="dev")  # must NOT raise


@pytest.mark.django_db
def test_same_tag_machine_channel_conflicts():
    _mk(channel="dev")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _mk(channel="dev")


@pytest.mark.django_db
def test_latest_is_per_machine_and_channel():
    a = _mk(channel="release", is_latest=True)
    b = _mk(tag="v1.0.1", channel="dev", is_latest=True)  # different channel, both latest OK
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.is_latest and b.is_latest


@pytest.mark.django_db
def test_new_latest_same_channel_flips_previous():
    a = _mk(tag="v1", channel="dev", is_latest=True)
    b = _mk(tag="v2", channel="dev", is_latest=True)
    a.refresh_from_db()
    assert not a.is_latest and b.is_latest
