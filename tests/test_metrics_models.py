"""Model invariants for generic metrics: threshold AlertRule, MetricDefinition, ThresholdState."""

import pytest
from django.db import IntegrityError, transaction

from apps.monitoring.models import (
    MAX_DEFINITIONS,
    Alert,
    AlertRule,
    MetricDefinition,
    ThresholdState,
)
from apps.stations.models import StationTelemetry
from station_agent.metrics_contract import GROUPS

pytestmark = pytest.mark.django_db


def _threshold_rule(**kw):
    defaults = {
        "alert_type": AlertRule.AlertType.THRESHOLD,
        "metric_key": "test.value_pct",
        "operator": AlertRule.Operator.GTE,
        "threshold": 50.0,
        "severity": AlertRule.Severity.WARNING,
    }
    defaults.update(kw)
    return AlertRule.objects.create(**defaults)


def test_legacy_alert_types_removed():
    values = set(AlertRule.AlertType.values)
    assert {"cpu_temperature", "disk_warning", "disk_critical", "ram_critical"}.isdisjoint(values)
    assert "threshold" in values
    assert "station_offline" in values


def test_event_type_still_unique():
    AlertRule.objects.filter(alert_type="ota_failed").delete()
    AlertRule.objects.create(alert_type="ota_failed", threshold=0, severity="critical")
    with pytest.raises(IntegrityError), transaction.atomic():
        AlertRule.objects.create(alert_type="ota_failed", threshold=0, severity="warning")


def test_multiple_threshold_rules_allowed():
    a = _threshold_rule(threshold=90.0)
    b = _threshold_rule(threshold=95.0, severity=AlertRule.Severity.CRITICAL)
    assert a.pk != b.pk


def test_threshold_rule_defaults_and_str():
    rule = _threshold_rule()
    assert rule.aggregate == AlertRule.Aggregate.AVG
    assert rule.duration_seconds == 0
    assert str(rule) == "test.value_pct gte 50.0"
    rule.name = "Test value"
    assert str(rule) == "Test value gte 50.0"


def test_operator_and_aggregate_choices():
    assert set(AlertRule.Operator.values) == {"gt", "gte", "lt", "lte"}
    assert set(AlertRule.Aggregate.values) == {"avg", "min", "max"}


def test_metric_definition_display_label_resolution():
    d = MetricDefinition.objects.create(key="x.y_v", label="Agent label")
    assert d.display_label == "Agent label"
    d.label_override = "Operator label"
    assert d.display_label == "Operator label"
    bare = MetricDefinition(key="x.z_v")
    assert bare.display_label == "x.z_v"


def test_metric_definition_range_override_wins_incl_zero():
    d = MetricDefinition.objects.create(key="pwr.12v.current_a", min_value=-8.0, max_value=8.0)
    assert (d.range_min, d.range_max) == (-8.0, 8.0)
    d.min_override = 0.0
    d.max_override = 0.0
    assert (d.range_min, d.range_max) == (0.0, 0.0)
    d.min_override = None
    assert d.range_min == -8.0


def test_metric_definition_defaults_and_groups():
    d = MetricDefinition.objects.create(key="a.b")
    assert d.group == "other"
    assert d.precision == 1
    assert d.is_hidden is False and d.is_blocked is False
    assert d.first_seen is not None and d.last_seen is not None
    assert d.last_rejected_at is None and d.last_rejected_value is None
    assert set(dict(MetricDefinition._meta.get_field("group").choices)) == set(GROUPS)
    assert MAX_DEFINITIONS == 2000


def test_metric_definition_key_unique():
    MetricDefinition.objects.create(key="dup.key")
    with pytest.raises(IntegrityError), transaction.atomic():
        MetricDefinition.objects.create(key="dup.key")


def test_threshold_state_unique_and_cascade(station):
    rule = _threshold_rule()
    ThresholdState.objects.create(rule=rule, station=station, metric_key="test.value_pct")
    with pytest.raises(IntegrityError), transaction.atomic():
        ThresholdState.objects.create(rule=rule, station=station, metric_key="test.value_pct")
    ThresholdState.objects.create(rule=rule, station=station, metric_key="test.other_pct")
    rule.delete()
    assert ThresholdState.objects.count() == 0


def test_threshold_state_alert_set_null(station):
    rule = _threshold_rule()
    alert = Alert.objects.create(
        station=station, alert_rule=rule, severity="warning", title="t", message="m"
    )
    state = ThresholdState.objects.create(
        rule=rule, station=station, metric_key="k.v", alert=alert
    )
    alert.delete()
    state.refresh_from_db()
    assert state.alert is None
    assert state.breach_since is None and state.ok_since is None


def test_station_telemetry_metrics_fields(station):
    tel = StationTelemetry.objects.create(station=station)
    assert tel.metrics == {}
    assert tel.metric_descriptors_hash == ""
    tel.metrics = {
        "cpu.temp_c": {"min": 1.0, "max": 1.0, "avg": 1.0, "ts": "2026-11-03T00:00:00Z"}
    }
    tel.metric_descriptors_hash = "a" * 64
    tel.save()
    tel.refresh_from_db()
    assert tel.metrics["cpu.temp_c"]["avg"] == 1.0


def test_threshold_state_admin_changelist_no_n_plus_one(admin_client, station_factory):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from django.urls import reverse

    rule = _threshold_rule()
    url = reverse("admin:monitoring_thresholdstate_changelist")

    def add_states(n):
        for _ in range(n):
            st = station_factory()
            alert = Alert.objects.create(
                station=st, alert_rule=rule, severity="warning", title="t", message="m"
            )
            ThresholdState.objects.create(rule=rule, station=st, metric_key="k.v", alert=alert)

    add_states(2)
    with CaptureQueriesContext(connection) as small:
        assert admin_client.get(url).status_code == 200
    add_states(4)
    with CaptureQueriesContext(connection) as large:
        assert admin_client.get(url).status_code == 200
    assert len(large.captured_queries) == len(small.captured_queries)
