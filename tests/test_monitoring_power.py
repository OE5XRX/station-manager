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


# M4: escalation — WARNING must escalate to CRITICAL when *_now bit fires
@pytest.mark.django_db
def test_power_warning_escalates_to_critical_when_now_fires(power_rule):
    """M4: existing WARNING alert escalates to CRITICAL when undervoltage_now becomes True."""
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        undervoltage_occurred=True,
        undervoltage_now=False,
    )
    # First call: creates WARNING
    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert alerts[0].severity == Alert.Severity.WARNING

    # Simulate *_now bit firing
    tel.undervoltage_now = True
    tel.save()

    # Second call: should escalate the existing alert to CRITICAL
    alerts2 = _check_power_warning()
    assert len(alerts2) == 1
    assert alerts2[0].severity == Alert.Severity.CRITICAL
    # Verify the DB record was updated
    from apps.monitoring.models import Alert as AlertModel

    db_alert = AlertModel.objects.get(station=station, is_resolved=False)
    assert db_alert.severity == AlertModel.Severity.CRITICAL


# M5: auto-resolve when *_now bits are false (even if occurred is True)
@pytest.mark.django_db
def test_power_alert_resolves_when_now_bits_clear(power_rule):
    """M5: alert auto-resolves when both *_now bits are False, regardless of occurred."""
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        undervoltage_occurred=True,
        undervoltage_now=True,
    )
    # Create an active alert
    alerts = _check_power_warning()
    assert len(alerts) == 1

    # Now bits clear but occurred stays True (sticky bit)
    tel.undervoltage_now = False
    tel.throttled_now = False
    tel.save()

    # Second call: alert should be auto-resolved
    _check_power_warning()
    from apps.monitoring.models import Alert as AlertModel

    assert not AlertModel.objects.filter(station=station, is_resolved=False).exists()
