"""Tests for _check_power_warning (Task 14)."""

import pytest

from apps.monitoring.engine import _check_power_warning
from apps.monitoring.models import Alert, AlertRule
from apps.stations.models import Station, StationTelemetry


@pytest.fixture
def power_rule(db):
    rule, _ = AlertRule.objects.get_or_create(
        alert_type=AlertRule.AlertType.POWER_WARNING,
        defaults={
            "threshold": 0,
            "severity": AlertRule.Severity.WARNING,
            "is_active": True,
        },
    )
    return rule


@pytest.mark.django_db
def test_undervoltage_now_is_critical(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        undervoltage_now=True,
        undervoltage_occurred=True,
    )
    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert alerts[0].severity == Alert.Severity.CRITICAL


@pytest.mark.django_db
def test_occurred_only_is_warning(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        undervoltage_now=False,
        undervoltage_occurred=True,
    )
    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert alerts[0].severity == Alert.Severity.WARNING


@pytest.mark.django_db
def test_no_power_issue_no_alert(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        undervoltage_occurred=False,
        throttled_occurred=False,
    )
    assert _check_power_warning() == []


@pytest.mark.django_db
def test_power_dedup(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(station=station, undervoltage_occurred=True)
    assert len(_check_power_warning()) == 1
    assert _check_power_warning() == []
