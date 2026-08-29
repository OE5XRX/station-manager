"""Tests for the 'Send test push' channel.

Mirrors the email/telegram test-notification path: send_test_notification
routes "webpush" to _test_webpush, which pushes ONLY to the requesting
user's own registered devices and returns a clear (success, error) tuple.
"""

from unittest import mock

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.monitoring.notifications import send_test_notification
from apps.webpush.models import PushSubscription


def _admin(username="admin", email="admin@x"):
    u = User.objects.create_user(username=username, password="x", email=email)
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    return u


def _sub(user):
    return PushSubscription.objects.create(
        user=user, endpoint=f"https://push.example/{user.pk}", p256dh="p", auth="a"
    )


@pytest.mark.django_db
def test_webpush_disabled_returns_error(settings):
    settings.ALERT_WEBPUSH_ENABLED = False
    u = _admin()
    _sub(u)
    ok, err = send_test_notification("webpush", requesting_user=u)
    assert ok is False
    assert "not enabled" in err


@pytest.mark.django_db
def test_webpush_without_device_returns_error(settings):
    settings.ALERT_WEBPUSH_ENABLED = True
    u = _admin()  # no subscription
    ok, err = send_test_notification("webpush", requesting_user=u)
    assert ok is False
    assert "No push device" in err


@pytest.mark.django_db
def test_webpush_sends_only_to_requesting_user(settings):
    settings.ALERT_WEBPUSH_ENABLED = True
    me = _admin("me", "me@x")
    other = _admin("other", "other@x")
    _sub(me)
    _sub(other)
    with mock.patch("apps.monitoring.notifications.send_web_push", return_value=True) as m:
        ok, err = send_test_notification("webpush", requesting_user=me)
    assert ok is True and err == ""
    # exactly one push, to the requesting user's own subscription
    assert m.call_count == 1
    sent_sub = m.call_args.args[0]
    assert sent_sub.user_id == me.id


@pytest.mark.django_db
def test_webpush_all_devices_fail_returns_error(settings):
    settings.ALERT_WEBPUSH_ENABLED = True
    u = _admin()
    _sub(u)
    with mock.patch("apps.monitoring.notifications.send_web_push", return_value=False):
        ok, err = send_test_notification("webpush", requesting_user=u)
    assert ok is False
    assert "failed" in err


@pytest.mark.django_db
def test_test_push_view_posts_and_returns_json(client, settings):
    settings.ALERT_WEBPUSH_ENABLED = True
    u = _admin()
    _sub(u)
    client.force_login(u)
    with mock.patch("apps.monitoring.notifications.send_web_push", return_value=True):
        r = client.post(reverse("monitoring:test_push"))
    assert r.status_code == 200
    assert r.json()["success"] is True


@pytest.mark.django_db
def test_test_push_view_requires_admin(client, settings):
    settings.ALERT_WEBPUSH_ENABLED = True
    member = User.objects.create_user(username="m", password="x", email="m@x")
    client.force_login(member)
    r = client.post(reverse("monitoring:test_push"))
    # AdminRequiredMixin blocks non-admins (redirect or 403)
    assert r.status_code in (302, 403)
