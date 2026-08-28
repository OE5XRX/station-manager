import pytest

from apps.accounts.models import User
from apps.monitoring.recipients import (
    email_recipients_for_station_alert,
    push_recipients_for_station_alert,
)
from apps.stations.models import Station
from apps.webpush.models import PushSubscription


def _admin(username, channel, email=None):
    email = email or f"{username}@x"
    u = User.objects.create_user(username=username, password="x", email=email)
    u.membership_level = User.MembershipLevel.ADMIN
    u.notify_channel = channel
    u.save(update_fields=["membership_level", "notify_channel"])
    return u


def _sub(u):
    return PushSubscription.objects.create(
        user=u, endpoint=f"https://push.example/{u.pk}", p256dh="p", auth="a"
    )


@pytest.mark.django_db
def test_email_user_only_in_email_set():
    u = _admin("a", User.NotifyChannel.EMAIL)
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    assert u in list(email_recipients_for_station_alert(s))
    assert u not in list(push_recipients_for_station_alert(s))


@pytest.mark.django_db
def test_push_user_with_device_only_in_push_set():
    u = _admin("b", User.NotifyChannel.PUSH)
    _sub(u)
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    assert u in list(push_recipients_for_station_alert(s))
    assert u not in list(email_recipients_for_station_alert(s))


@pytest.mark.django_db
def test_push_user_without_device_falls_back_to_email():
    u = _admin("c", User.NotifyChannel.PUSH)  # no subscription
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    assert u in list(email_recipients_for_station_alert(s))
    assert u not in list(push_recipients_for_station_alert(s))


@pytest.mark.django_db
def test_both_user_with_device_in_both_sets():
    u = _admin("d", User.NotifyChannel.BOTH)
    _sub(u)
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    assert u in list(email_recipients_for_station_alert(s))
    assert u in list(push_recipients_for_station_alert(s))
