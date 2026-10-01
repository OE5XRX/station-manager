"""Tests for _check_unexpected_reboot (Task 13)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.monitoring.engine import _check_unexpected_reboot
from apps.monitoring.models import AlertRule
from apps.stations.models import Station, StationTelemetry


@pytest.fixture
def reboot_rule(db):
    rule, _ = AlertRule.objects.get_or_create(
        alert_type=AlertRule.AlertType.UNEXPECTED_REBOOT,
        defaults={
            "threshold": 0,
            "severity": AlertRule.Severity.WARNING,
            "is_active": True,
        },
    )
    return rule


@pytest.mark.django_db
def test_crash_reboot_raises_alert(reboot_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        last_reboot_at=timezone.now(),
        last_reboot_reason="crash",
        boot_count=5,
    )
    alerts = _check_unexpected_reboot()
    assert len(alerts) == 1
    assert alerts[0].station == station


@pytest.mark.django_db
def test_clean_reboot_no_alert(reboot_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        last_reboot_at=timezone.now(),
        last_reboot_reason="clean",
    )
    assert _check_unexpected_reboot() == []


@pytest.mark.django_db
def test_same_reboot_dedup(reboot_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        last_reboot_at=timezone.now(),
        last_reboot_reason="crash",
    )
    first = _check_unexpected_reboot()
    assert len(first) == 1
    assert _check_unexpected_reboot() == []  # same reboot, no dupe


@pytest.mark.django_db
def test_second_distinct_reboot_realerts(reboot_rule):
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        last_reboot_at=timezone.now() - timedelta(minutes=1),
        last_reboot_reason="crash",
    )
    _check_unexpected_reboot()
    # A new reboot happens (newer last_reboot_at), first alert still unresolved.
    tel.last_reboot_at = timezone.now()
    tel.save(update_fields=["last_reboot_at"])
    second = _check_unexpected_reboot()
    assert len(second) == 1
