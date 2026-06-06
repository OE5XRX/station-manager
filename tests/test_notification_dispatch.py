"""Tests for the notification dispatch wiring.

Verifies _send_email_notification routes via recipients_for_station_alert
and that the test-email endpoint sends only to the requesting user.
"""

import logging

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import User
from apps.monitoring.models import Alert, AlertRule
from apps.monitoring.notifications import send_alert_notifications
from apps.stations.models import Station, StationAssignment


def _user(level, email, username):
    u = User.objects.create_user(username=username, password="x", email=email)
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
def test_alert_email_goes_to_station_admin_and_vereins_admin(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []

    _user(User.MembershipLevel.ADMIN, "admin@x", "admin")
    station_admin = _user(User.MembershipLevel.MEMBER, "franz@x", "franz")

    s = Station.objects.create(name="OE5A", callsign="OE5A")
    StationAssignment.objects.create(
        user=station_admin,
        station=s,
        role=StationAssignment.Role.ADMIN,
    )
    rule = AlertRule.objects.get(alert_type=AlertRule.AlertType.STATION_OFFLINE)
    alert = Alert.objects.create(
        station=s,
        alert_rule=rule,
        severity="critical",
        title="Test",
        message="m",
    )

    send_alert_notifications(alert)

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert set(sent.to) == {"admin@x", "franz@x"}


@pytest.mark.django_db
def test_test_email_goes_only_to_requesting_admin(client, settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []

    admin1 = _user(User.MembershipLevel.ADMIN, "admin1@x", "admin1")
    _user(User.MembershipLevel.ADMIN, "admin2@x", "admin2")

    client.force_login(admin1)
    response = client.post(reverse("monitoring:test_email"))
    assert response.status_code == 200
    assert response.json()["success"] is True

    assert len(mail.outbox) == 1
    assert list(mail.outbox[0].to) == ["admin1@x"]


@pytest.mark.django_db
def test_no_recipients_logs_warning_and_does_not_send(settings, caplog):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []

    # Station without region and no admin user in DB -> empty set
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    rule = AlertRule.objects.get(alert_type=AlertRule.AlertType.STATION_OFFLINE)
    alert = Alert.objects.create(
        station=s,
        alert_rule=rule,
        severity="critical",
        title="Test",
        message="m",
    )

    with caplog.at_level(logging.WARNING, logger="apps.monitoring.notifications"):
        send_alert_notifications(alert)

    assert len(mail.outbox) == 0
    assert any("no recipients" in rec.message.lower() for rec in caplog.records)
