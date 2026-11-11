"""monitoring.0008: the special-cased ``passive_no_rx`` alert becomes a generic rule."""

import pytest
from django.utils import timezone

from apps.monitoring import engine
from apps.monitoring.models import Alert, AlertRule, MetricDefinition, ThresholdState
from apps.stations.models import StationTelemetry

PRE = [("monitoring", "0007_passive_no_rx_alert_type")]
TESTED = [("monitoring", "0008_aprs_rx_threshold_rule")]
PASSIVE_PRE = [("passive", "0004_aprspacket_pending_idx")]
PASSIVE_TESTED = [("passive", "0005_remove_aprssettings_no_rx_alert_hours")]


def _seed(apps, *, with_generic=False):
    AlertRule = apps.get_model("monitoring", "AlertRule")
    Alert = apps.get_model("monitoring", "Alert")
    Station = apps.get_model("stations", "Station")
    old, _ = AlertRule.objects.get_or_create(
        alert_type="passive_no_rx",
        defaults={"threshold": 0.0, "severity": "warning", "is_active": True},
    )
    station = Station.objects.create(name="aprs-mig")
    alert = Alert.objects.create(
        station=station, alert_rule=old, severity="warning", title="t", message="m"
    )
    if with_generic:
        AlertRule.objects.create(
            alert_type="threshold",
            metric_key="aprs.rx_packets_1h",
            operator="lt",
            threshold=3.0,
            duration_seconds=60,
            severity="critical",
            name="tuned",
        )
    return alert.pk


def test_special_rule_replaced_and_open_alert_resolved(migrator):
    old = migrator.apply_initial_migration(PRE)
    alert_pk = _seed(old.apps)
    new = migrator.apply_tested_migration(TESTED)
    rule_model = new.apps.get_model("monitoring", "AlertRule")
    assert not rule_model.objects.filter(alert_type="passive_no_rx").exists()
    rule = rule_model.objects.get(alert_type="threshold", metric_key="aprs.rx_packets_1h")
    assert (rule.operator, rule.threshold, rule.aggregate) == ("lt", 1.0, "avg")
    assert rule.duration_seconds == 5 * 3600 and rule.is_active
    alert = new.apps.get_model("monitoring", "Alert").objects.get(pk=alert_pk)
    assert alert.is_resolved is True and alert.resolved_at is not None


def test_existing_generic_rule_is_kept_untouched(migrator):
    old = migrator.apply_initial_migration(PRE)
    _seed(old.apps, with_generic=True)
    new = migrator.apply_tested_migration(TESTED)
    rules = new.apps.get_model("monitoring", "AlertRule").objects.filter(
        metric_key="aprs.rx_packets_1h"
    )
    assert [(r.name, r.threshold) for r in rules] == [("tuned", 3.0)]


def test_aprs_settings_field_removed(migrator):
    def has_field(state):
        model = state.apps.get_model("passive", "AprsSettings")
        return any(f.name == "no_rx_alert_hours" for f in model._meta.get_fields())

    assert has_field(migrator.apply_initial_migration(PASSIVE_PRE))
    assert not has_field(migrator.apply_tested_migration(PASSIVE_TESTED))


def test_reverse_restores_special_rule(migrator):
    migrator.apply_initial_migration(TESTED)
    old = migrator.apply_tested_migration(PRE)
    assert (
        old.apps.get_model("monitoring", "AlertRule")
        .objects.filter(alert_type="passive_no_rx")
        .exists()
    )


# --- engine behaviour on the seeded rule (fresh DB = all migrations applied) -------------


@pytest.mark.django_db
def test_seeded_rule_fires_after_5h_and_freezes_without_key(station):
    from datetime import timedelta

    rule = AlertRule.objects.get(alert_type="threshold", metric_key="aprs.rx_packets_1h")
    MetricDefinition.objects.get_or_create(
        key="aprs.rx_packets_1h", defaults={"label": "APRS packets heard (1 h)", "unit": "pkts"}
    )
    t0 = timezone.now()
    station.last_seen = t0
    station.save(update_fields=["last_seen"])

    def heartbeat(at, **metrics):
        station.last_seen = at
        station.save(update_fields=["last_seen"])
        StationTelemetry.objects.update_or_create(
            station=station,
            defaults={
                "metrics": {
                    k: {"min": v, "max": v, "avg": v, "ts": at.isoformat()}
                    for k, v in metrics.items()
                }
            },
        )

    heartbeat(t0, **{"aprs.rx_packets_1h": 0.0})
    assert engine._check_thresholds(now=t0) == []
    # decoder stopped / used by a human: key absent -> frozen, no alert even after 6 h
    heartbeat(t0 + timedelta(hours=6))
    assert engine._check_thresholds(now=t0 + timedelta(hours=6)) == []
    assert not Alert.objects.filter(alert_rule=rule).exists()
    # still silent when the key is back; the gap never counted
    t1 = t0 + timedelta(hours=6)
    heartbeat(t1, **{"aprs.rx_packets_1h": 0.0})
    assert engine._check_thresholds(now=t1) == []
    t2 = t1 + timedelta(hours=5)
    heartbeat(t2, **{"aprs.rx_packets_1h": 0.0})
    # keep the state "evaluated" across the long gap in this synthetic test
    ThresholdState.objects.filter(rule=rule).update(last_evaluated_at=t2 - timedelta(minutes=1))
    assert len(engine._check_thresholds(now=t2)) == 1
    assert Alert.objects.filter(alert_rule=rule, is_resolved=False).count() == 1
