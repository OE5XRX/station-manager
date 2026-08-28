# tests/test_webpush_dispatch.py
from unittest import mock

import pytest

from apps.accounts.models import User
from apps.webpush import dispatch
from apps.webpush.models import PushSubscription


def _sub(endpoint="https://push.example/x"):
    u = User.objects.create_user(username="a", password="x", email="a@x")
    return PushSubscription.objects.create(user=u, endpoint=endpoint, p256dh="p", auth="a")


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.django_db
def test_success_updates_timestamps(settings):
    settings.WEBPUSH_VAPID_PRIVATE_KEY = "priv"
    settings.WEBPUSH_VAPID_ADMIN_EMAIL = "mailto:a@x"
    s = _sub()
    with mock.patch.object(dispatch, "webpush") as m:
        ok = dispatch.send_web_push(s, {"title": "t", "body": "b"})
    assert ok is True
    m.assert_called_once()
    s.refresh_from_db()
    assert s.last_success_at is not None
    assert s.failure_count == 0


@pytest.mark.django_db
def test_expired_subscription_is_deleted(settings):
    settings.WEBPUSH_VAPID_PRIVATE_KEY = "priv"
    settings.WEBPUSH_VAPID_ADMIN_EMAIL = "mailto:a@x"
    s = _sub()
    exc = dispatch.WebPushException("gone", response=_Resp(410))
    with mock.patch.object(dispatch, "webpush", side_effect=exc):
        ok = dispatch.send_web_push(s, {"title": "t"})
    assert ok is False
    assert not PushSubscription.objects.filter(pk=s.pk).exists()


@pytest.mark.django_db
def test_transient_error_increments_failure(settings):
    settings.WEBPUSH_VAPID_PRIVATE_KEY = "priv"
    settings.WEBPUSH_VAPID_ADMIN_EMAIL = "mailto:a@x"
    s = _sub()
    exc = dispatch.WebPushException("boom", response=_Resp(500))
    with mock.patch.object(dispatch, "webpush", side_effect=exc):
        ok = dispatch.send_web_push(s, {"title": "t"})
    assert ok is False
    s.refresh_from_db()
    assert s.failure_count == 1


@pytest.mark.django_db
def test_generic_error_increments_failure_and_keeps_subscription(settings):
    settings.WEBPUSH_VAPID_PRIVATE_KEY = "priv"
    settings.WEBPUSH_VAPID_ADMIN_EMAIL = "mailto:a@x"
    s = _sub()
    with mock.patch.object(dispatch, "webpush", side_effect=ConnectionError("boom")):
        ok = dispatch.send_web_push(s, {"title": "t"})
    assert ok is False
    assert PushSubscription.objects.filter(pk=s.pk).exists()
    s.refresh_from_db()
    assert s.failure_count == 1
