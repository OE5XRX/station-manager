"""Smoke test: new alert types route via existing topology notification channels (Task 16)."""

import pytest
from django.core import mail

from apps.accounts.models import User
from apps.monitoring.models import Alert, AlertRule
from apps.monitoring.notifications import send_alert_notifications
from apps.stations.models import Station, StationAssignment


@pytest.mark.django_db
def test_unexpected_reboot_alert_emails_station_admin(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []

    admin = User.objects.create_user(
        username="sa", email="sa@x", password="pw",
    )
    admin.membership_level = User.MembershipLevel.MEMBER
    admin.notify_channel = User.NotifyChannel.EMAIL
    admin.save(update_fields=["membership_level", "notify_channel"])

    station = Station.objects.create(name="OE5A")
    StationAssignment.objects.create(
        user=admin,
        station=station,
        role=StationAssignment.Role.ADMIN,
    )
    rule, _ = AlertRule.objects.get_or_create(
        alert_type=AlertRule.AlertType.UNEXPECTED_REBOOT,
        defaults={
            "threshold": 0,
            "severity": AlertRule.Severity.WARNING,
            "is_active": True,
        },
    )
    alert = Alert.objects.create(
        station=station,
        alert_rule=rule,
        severity=Alert.Severity.WARNING,
        title="Unexpected reboot: crash",
        message="boom",
    )
    send_alert_notifications(alert)
    assert any("sa@x" in m.to for m in mail.outbox)
