import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _mk(level, name=None):
    return User.objects.create_user(
        username=name or f"u-{level}", password="x", membership_level=level
    )


def test_member_sees_directory_and_sidebar_link(client):
    client.force_login(_mk("member"))
    r = client.get(reverse("accounts:user_list"))
    assert r.status_code == 200
    assert reverse("accounts:user_list") in client.get(reverse("dashboard:index")).content.decode()


def test_applicant_directory_403_but_own_detail_ok(client):
    me = _mk("applicant")
    client.force_login(me)
    assert client.get(reverse("accounts:user_list")).status_code == 403
    assert client.get(reverse("accounts:user_detail", args=[me.pk])).status_code == 200


def test_applicant_other_user_detail_is_403(client):
    # Area gate (users.view needs Member) fires before visibility's 404.
    client.force_login(_mk("applicant"))
    other = _mk("member", "other")
    assert client.get(reverse("accounts:user_detail", args=[other.pk])).status_code == 403


def test_member_cannot_open_applicant_detail(client):
    # Area gate passes (users.view), visibility hides the Applicant -> 404.
    client.force_login(_mk("member"))
    app = _mk("applicant", "app")
    assert client.get(reverse("accounts:user_detail", args=[app.pk])).status_code == 404


def test_member_directory_hides_applicants(client):
    client.force_login(_mk("member"))
    _mk("applicant", "hidden-app")
    assert "hidden-app" not in client.get(reverse("accounts:user_list")).content.decode()


def test_staff_cannot_edit_users(client):
    client.force_login(_mk("staff"))
    target = _mk("member", "tgt")
    assert client.get(reverse("accounts:user_edit", args=[target.pk])).status_code == 403


def test_staff_user_list_is_read_only(client):
    client.force_login(_mk("staff"))
    r = client.get(reverse("accounts:user_list"))
    assert r.status_code == 200
    assert reverse("accounts:user_create") not in r.content.decode()


def test_applicant_cannot_open_api_tokens(client):
    client.force_login(_mk("applicant"))
    assert client.get(reverse("accounts:api_tokens")).status_code == 403
    assert client.post(reverse("accounts:api_tokens"), {"name": "x"}).status_code == 403


def test_applicant_can_change_own_profile(client):
    client.force_login(_mk("applicant"))
    assert client.get(reverse("accounts:profile")).status_code == 200
    assert client.get(reverse("accounts:notification_settings")).status_code == 200


def test_staff_does_not_get_admin_audience(db):
    from apps.accounts.visibility import Audience, audience_for

    assert audience_for(_mk("staff"), _mk("member", "x")) == Audience.MEMBER


def test_directory_queryset(db):
    from apps.accounts.visibility import directory_queryset

    app = _mk("applicant", "a1")
    mem = _mk("member", "m1")
    adm = _mk("admin", "ad1")
    gone = _mk("member", "gone")
    from django.utils import timezone

    gone.deleted_at = timezone.now()
    gone.save()
    assert set(directory_queryset(mem)) == {mem, adm}
    assert set(directory_queryset(adm)) == {app, mem, adm}


def test_webpush_subscribe_requires_profile_use(client):
    from django.test import Client

    r = Client().post(reverse("webpush:subscribe"), "{}", content_type="application/json")
    assert r.status_code == 302
