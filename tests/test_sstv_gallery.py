"""SSTV gallery: partial (read-only for viewers), picture/download views, delete rules."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import User
from apps.sstv.models import SstvImage
from apps.sstv.views_gallery import PAGE_SIZE, download_filename
from apps.stations.models import Station

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\nfake"


def _user(level, name=None):
    return User.objects.create_user(
        username=name or f"g-{level}", password="x", membership_level=level
    )


def _image(station, *, direction="rx", user=None, complete=True, mode="martin1", **kw):
    img = SstvImage(
        station=station, direction=direction, mode=mode, user=user, complete=complete, **kw
    )
    img.image.save("p.png", ContentFile(PNG), save=False)
    img.save()
    return img


def _gallery(client, station, **params):
    return client.get(reverse("sstv:gallery", args=[station.pk]), params)


# ---------------------------------------------------------------------------
# Gallery partial
# ---------------------------------------------------------------------------


def test_gallery_anonymous_redirects_to_login(client, station):
    resp = _gallery(client, station)
    assert resp.status_code == 302 and "/login" in resp["Location"]


def test_gallery_applicant_sees_pictures_read_only(client, station):
    member = _user("member")
    _image(station, direction="tx", user=member, callsign="oe5abc", frequency_hz=145_500_000)
    _image(station, direction="rx", user=member, complete=False)
    client.force_login(_user("applicant"))
    resp = _gallery(client, station)
    assert resp.status_code == 200
    html = resp.content.decode()
    assert html.count('class="sg-tile') == 2
    assert reverse("sstv:download", args=[station.pk, SstvImage.objects.first().pk]) in html
    assert "/delete/" not in html and "hx-post" not in html
    assert "oe5abc" in html
    assert "145.500" in html  # en locale


def test_gallery_newest_first_and_only_this_station(client, station, station_factory):
    other = station_factory()
    old = _image(station)
    new = _image(station)
    SstvImage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=1))
    foreign = _image(other)
    client.force_login(_user("applicant"))
    html = _gallery(client, station).content.decode()
    assert html.index(str(new.pk)) < html.index(str(old.pk))
    assert str(foreign.pk) not in html


def test_gallery_unknown_station_404(client):
    client.force_login(_user("applicant"))
    assert client.get(reverse("sstv:gallery", args=[99999])).status_code == 404


def test_gallery_empty_state(client, station):
    client.force_login(_user("applicant"))
    html = _gallery(client, station).content.decode()
    assert "sg-empty" in html and 'class="sg-tile' not in html


def test_gallery_pages_of_24_with_load_more(client, station):
    base = timezone.now()
    imgs = [_image(station) for _ in range(PAGE_SIZE + 3)]
    for i, img in enumerate(imgs):
        SstvImage.objects.filter(pk=img.pk).update(created_at=base - timedelta(minutes=i))
    client.force_login(_user("applicant"))
    first = _gallery(client, station).content.decode()
    assert PAGE_SIZE == 24
    assert first.count('class="sg-tile') == PAGE_SIZE
    assert "page=2" in first and 'id="sstv-gallery"' in first
    second = _gallery(client, station, page=2).content.decode()
    assert second.count('class="sg-tile') == 3
    assert "page=3" not in second
    # Later pages are bare tiles for the "load more" swap, no second wrapper.
    assert 'id="sstv-gallery"' not in second
    shown = {str(i.pk) for i in imgs[:PAGE_SIZE]}
    assert all(pk in first for pk in shown)
    assert all(str(i.pk) in second for i in imgs[PAGE_SIZE:])


@pytest.mark.parametrize("page", ["0", "-1", "abc", "999999999999999999999"])
def test_gallery_bad_page_param_does_not_crash(client, station, page):
    _image(station)
    client.force_login(_user("applicant"))
    assert _gallery(client, station, page=page).status_code == 200


def test_gallery_member_sees_delete_only_on_own_tx(client, station):
    member = _user("member")
    other = _user("member", "g-other")
    own_tx = _image(station, direction="tx", user=member)
    other_tx = _image(station, direction="tx", user=other)
    own_rx = _image(station, direction="rx", user=member)
    client.force_login(member)
    html = _gallery(client, station).content.decode()
    assert reverse("sstv:delete", args=[station.pk, own_tx.pk]) in html
    assert reverse("sstv:delete", args=[station.pk, other_tx.pk]) not in html
    assert reverse("sstv:delete", args=[station.pk, own_rx.pk]) not in html


def test_gallery_staff_sees_delete_on_every_picture(client, station):
    imgs = [_image(station, direction="tx"), _image(station, direction="rx")]
    client.force_login(_user("staff"))
    html = _gallery(client, station).content.decode()
    for img in imgs:
        assert reverse("sstv:delete", args=[station.pk, img.pk]) in html


def test_gallery_has_no_storage_urls(client, station):
    """Pictures are served through the permission-checked view, never a storage URL."""
    img = _image(station)
    client.force_login(_user("applicant"))
    html = _gallery(client, station).content.decode()
    assert reverse("sstv:image", args=[station.pk, img.pk]) in html
    assert img.image.name not in html


def test_gallery_german(client, station):
    _image(station, complete=False, frequency_hz=145_800_000)
    user = _user("applicant")
    user.language = "de"
    user.save()
    client.force_login(user)
    client.cookies["django_language"] = "de"
    with translation.override("de"):  # the request activates "de"; restore afterwards
        html = client.get(reverse("sstv:gallery", args=[station.pk])).content.decode()
    assert "Unvollständig" in html
    assert "Herunterladen" in html


# ---------------------------------------------------------------------------
# Picture + download
# ---------------------------------------------------------------------------


def test_download_headers(client, station):
    img = _image(station, direction="tx", mode="pd120")
    stamp = datetime(2026, 7, 20, 9, 5, 7, tzinfo=UTC)
    SstvImage.objects.filter(pk=img.pk).update(created_at=stamp)
    client.force_login(_user("applicant"))
    resp = client.get(reverse("sstv:download", args=[station.pk, img.pk]))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/png"
    assert resp["Content-Disposition"] == (
        'attachment; filename="test-station-20260720-090507Z-pd120-tx.png"'
    )
    assert b"".join(resp.streaming_content) == PNG


def test_inline_picture(client, station):
    img = _image(station)
    client.force_login(_user("applicant"))
    resp = client.get(reverse("sstv:image", args=[station.pk, img.pk]))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/png"
    assert resp["Content-Disposition"].startswith("inline")
    assert "private" in resp["Cache-Control"]
    assert b"".join(resp.streaming_content) == PNG


def test_download_filename_uses_utc_and_slug():
    st = Station(pk=7, name="Gaisberg / Süd")
    img = SstvImage(
        station=st,
        direction="rx",
        mode="scottiedx",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC) + timedelta(hours=0),
    )
    assert download_filename(img) == "gaisberg-sud-20260102-030405Z-scottiedx-rx.png"
    # A name without slug characters still yields a usable, header-safe name.
    img.station = Station(pk=7, name='"; ;')
    assert download_filename(img) == "station-7-20260102-030405Z-scottiedx-rx.png"


def test_picture_of_other_station_is_404(client, station, station_factory):
    img = _image(station_factory())
    client.force_login(_user("applicant"))
    for name in ("sstv:image", "sstv:download"):
        assert client.get(reverse(name, args=[station.pk, img.pk])).status_code == 404


def test_picture_with_missing_file_is_404(client, station):
    img = _image(station)
    default_storage.delete(img.image.name)
    client.force_login(_user("applicant"))
    assert client.get(reverse("sstv:download", args=[station.pk, img.pk])).status_code == 404


def test_download_anonymous_redirects(client, station):
    img = _image(station)
    resp = client.get(reverse("sstv:download", args=[station.pk, img.pk]))
    assert resp.status_code == 302 and "/login" in resp["Location"]


# ---------------------------------------------------------------------------
# Delete rules
# ---------------------------------------------------------------------------


def _delete(client, station, img_pk, **headers):
    return client.post(reverse("sstv:delete", args=[station.pk, img_pk]), **headers)


@pytest.mark.parametrize(
    "level,direction,own,allowed",
    [
        ("applicant", "tx", True, False),
        ("member", "tx", True, True),
        ("member", "tx", False, False),
        ("member", "rx", True, False),
        ("member", "rx", False, False),
        ("staff", "tx", False, True),
        ("staff", "rx", False, True),
        ("admin", "rx", False, True),
    ],
)
def test_delete_matrix(
    client, station, django_capture_on_commit_callbacks, level, direction, own, allowed
):
    actor = _user(level)
    owner = actor if own else _user("member", "g-owner")
    img = _image(station, direction=direction, user=owner)
    name = img.image.name
    client.force_login(actor)
    with django_capture_on_commit_callbacks(execute=True):
        resp = _delete(client, station, img.pk, HTTP_HX_REQUEST="true")
    if allowed:
        assert resp.status_code == 200 and resp.content == b""
        assert not SstvImage.objects.filter(pk=img.pk).exists()
        assert not default_storage.exists(name)
    else:
        assert resp.status_code == 403
        assert SstvImage.objects.filter(pk=img.pk).exists()
        assert default_storage.exists(name)


def test_delete_own_tx_after_user_lost_use_is_403(client, station):
    """Own TX needs sstv.use *now* — a demoted applicant cannot delete it."""
    user = _user("member")
    img = _image(station, direction="tx", user=user)
    user.membership_level = "applicant"
    user.save()
    client.force_login(user)
    assert _delete(client, station, img.pk).status_code == 403


def test_delete_tx_of_deleted_user_needs_manage(client, station):
    img = _image(station, direction="tx", user=None, callsign="gone")
    client.force_login(_user("member"))
    assert _delete(client, station, img.pk).status_code == 403


def test_delete_get_not_allowed(client, station):
    img = _image(station, direction="tx")
    client.force_login(_user("staff"))
    resp = client.get(reverse("sstv:delete", args=[station.pk, img.pk]))
    assert resp.status_code == 405
    assert SstvImage.objects.filter(pk=img.pk).exists()


def test_delete_other_station_or_unknown_is_404(client, station, station_factory):
    img = _image(station_factory())
    client.force_login(_user("staff"))
    assert _delete(client, station, img.pk).status_code == 404
    assert _delete(client, station, uuid.uuid4()).status_code == 404
    assert SstvImage.objects.filter(pk=img.pk).exists()


def test_delete_without_htmx_redirects_to_control_page(client, station):
    img = _image(station, direction="rx")
    client.force_login(_user("staff"))
    resp = _delete(client, station, img.pk)
    assert resp.status_code == 302
    assert resp["Location"] == reverse("control:station_control", args=[station.pk])
