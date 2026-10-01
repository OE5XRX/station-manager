"""Tests for _check_storage_health (Task 15)."""

import pytest

from apps.monitoring.engine import _check_storage_health
from apps.monitoring.models import Alert, AlertRule
from apps.stations.models import Station, StationTelemetry


@pytest.fixture
def storage_rule(db):
    rule, _ = AlertRule.objects.get_or_create(
        alert_type=AlertRule.AlertType.STORAGE_HEALTH,
        defaults={
            "threshold": 80,
            "severity": AlertRule.Severity.WARNING,
            "is_active": True,
        },
    )
    return rule


@pytest.mark.django_db
def test_pre_eol_urgent_is_critical(storage_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(station=station, worst_pre_eol="urgent")
    alerts = _check_storage_health()
    assert len(alerts) == 1
    assert alerts[0].severity == Alert.Severity.CRITICAL


@pytest.mark.django_db
def test_high_wear_over_threshold(storage_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(station=station, worst_life_time_pct=90)
    assert len(_check_storage_health()) == 1


@pytest.mark.django_db
def test_io_errors_raise(storage_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(station=station, io_error_count=3)
    assert len(_check_storage_health()) == 1


@pytest.mark.django_db
def test_healthy_storage_no_alert(storage_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        worst_pre_eol="normal",
        worst_life_time_pct=10,
        io_error_count=0,
    )
    assert _check_storage_health() == []


# M6: io_error_count baseline — only re-alert on an increase
@pytest.mark.django_db
def test_io_error_alert_fires_once_then_no_repeat(storage_rule):
    """M6: io_error_count=3 fires once; same count after resolve must NOT re-alert."""
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        io_error_count=3,
        alerted_io_error_count=0,
    )
    # First call: alerts
    alerts = _check_storage_health()
    assert len(alerts) == 1

    # Operator resolves the alert
    from apps.monitoring.models import Alert

    Alert.objects.filter(station=station).update(is_resolved=True)

    # Second call with same count: must NOT re-alert (baseline was recorded)
    tel.refresh_from_db()
    assert tel.alerted_io_error_count == 3
    alerts2 = _check_storage_health()
    assert alerts2 == []


@pytest.mark.django_db
def test_io_error_alert_fires_again_on_increase(storage_rule):
    """M6: after resolve, io_error_count increases to 5 → re-alerts."""
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        io_error_count=3,
        alerted_io_error_count=0,
    )
    # First alert
    alerts = _check_storage_health()
    assert len(alerts) == 1
    from apps.monitoring.models import Alert

    Alert.objects.filter(station=station).update(is_resolved=True)

    # Count increases
    tel.refresh_from_db()
    tel.io_error_count = 5
    tel.save()

    # Should fire again (5 > alerted_io_error_count=3)
    alerts2 = _check_storage_health()
    assert len(alerts2) == 1
