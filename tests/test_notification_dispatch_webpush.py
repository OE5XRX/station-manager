from unittest import mock

import pytest
from django.core import mail

from apps.accounts.models import User
from apps.monitoring.models import Alert, AlertRule
from apps.monitoring.notifications import send_alert_notifications
from apps.stations.models import Station
from apps.webpush.models import PushSubscription


def _admin(username, channel):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.notify_channel = channel
    u.save(update_fields=["membership_level", "notify_channel"])
    return u


def _alert(station):
    rule = AlertRule.objects.get(alert_type=AlertRule.AlertType.STATION_OFFLINE)
    return Alert.objects.create(
        station=station, alert_rule=rule, severity="critical", title="T", message="m"
    )


@pytest.mark.django_db
def test_both_channel_triggers_email_and_push(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.ALERT_WEBPUSH_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    u = _admin("a", User.NotifyChannel.BOTH)
    PushSubscription.objects.create(
        user=u, endpoint="https://push.example/a", p256dh="p", auth="a"
    )
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    with mock.patch("apps.monitoring.notifications.send_web_push", return_value=True) as m:
        send_alert_notifications(_alert(s))
    assert len(mail.outbox) == 1
    assert m.call_count == 1


@pytest.mark.django_db
def test_push_without_device_only_emails(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.ALERT_WEBPUSH_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    _admin("b", User.NotifyChannel.PUSH)  # no device → email fallback
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    with mock.patch("apps.monitoring.notifications.send_web_push", return_value=True) as m:
        send_alert_notifications(_alert(s))
    assert len(mail.outbox) == 1
    assert m.call_count == 0


@pytest.mark.django_db
def test_webpush_disabled_skips_push(settings):
    settings.ALERT_EMAIL_ENABLED = False
    settings.ALERT_WEBPUSH_ENABLED = False
    u = _admin("c", User.NotifyChannel.PUSH)
    PushSubscription.objects.create(
        user=u, endpoint="https://push.example/c", p256dh="p", auth="a"
    )
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    with mock.patch("apps.monitoring.notifications.send_web_push", return_value=True) as m:
        send_alert_notifications(_alert(s))
    assert m.call_count == 0


@pytest.mark.django_db
def test_push_url_uses_each_recipients_language_prefix(settings):
    settings.ALERT_EMAIL_ENABLED = False
    settings.ALERT_WEBPUSH_ENABLED = True
    de = _admin("de", User.NotifyChannel.PUSH)
    de.language = "de"
    de.save(update_fields=["language"])
    en = _admin("en", User.NotifyChannel.PUSH)
    for u in (de, en):
        PushSubscription.objects.create(
            user=u, endpoint=f"https://push.example/{u.username}", p256dh="p", auth="a"
        )
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    with mock.patch("apps.monitoring.notifications.send_web_push", return_value=True) as m:
        send_alert_notifications(_alert(s))
    urls = {call.args[0].endpoint: call.args[1]["url"] for call in m.call_args_list}
    assert urls["https://push.example/de"].startswith("/de/")
    assert urls["https://push.example/en"].startswith("/en/")
