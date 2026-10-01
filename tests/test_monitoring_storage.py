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
