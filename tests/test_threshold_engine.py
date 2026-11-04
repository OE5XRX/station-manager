"""Generic threshold engine: duration, hysteresis, freezing, rule-change reset (Task 4)."""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.monitoring import engine
from apps.monitoring.engine import _check_thresholds
from apps.monitoring.models import Alert, AlertRule, MetricDefinition, ThresholdState
from apps.monitoring.rules import (
    compare,
    format_duration,
    reset_rule_evaluation,
    validate_metric_key,
)
from apps.stations.models import StationTelemetry

# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _val(v, ts=None):
    ts = ts or timezone.now()
    return {"min": v, "max": v, "avg": v, "ts": ts.isoformat()}


@pytest.fixture
def isolated_rules(db):
    """Deactivate the seeded threshold rules (migration 0006) so each test
    only sees the rules it creates. ``.update()`` bypasses the signals."""
    AlertRule.objects.filter(alert_type=AlertRule.AlertType.THRESHOLD).update(is_active=False)


@pytest.fixture
def online_station(station):
    station.last_seen = timezone.now()
    station.save(update_fields=["last_seen"])
    return station


def _rule(metric_key, threshold, op="gte", duration=0, severity="warning", aggregate="avg"):
    return AlertRule.objects.create(
        alert_type=AlertRule.AlertType.THRESHOLD,
        name=f"{metric_key} {op} {threshold}",
        metric_key=metric_key,
        operator=op,
        aggregate=aggregate,
        threshold=threshold,
        duration_seconds=duration,
        severity=severity,
        is_active=True,
    )


def _set_metrics(station, **metrics):
    """Replace the station's metrics; kwargs use '__' for '.'.

    Samples are stamped with the station's last heartbeat (or now), like the
    ingest does."""
    ts = station.last_seen or timezone.now()
    data = {k.replace("__", "."): _val(v, ts) for k, v in metrics.items()}
    StationTelemetry.objects.update_or_create(station=station, defaults={"metrics": data})


def _open_alerts(rule):
    return Alert.objects.filter(alert_rule=rule, is_resolved=False)


def _touch(station, now):
    """Simulate a heartbeat at ``now``: bump last_seen and re-stamp the samples."""
    station.last_seen = now
    station.save(update_fields=["last_seen"])
    tel = StationTelemetry.objects.filter(station=station).first()
    if tel is not None and isinstance(tel.metrics, dict):
        tel.metrics = {
            k: {**v, "ts": now.isoformat()} if isinstance(v, dict) else v
            for k, v in tel.metrics.items()
        }
        tel.save(update_fields=["metrics"])


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


class TestCompare:
    @pytest.mark.parametrize(
        "op,value,threshold,expected",
        [
            ("gt", 80.0, 80.0, False),
            ("gt", 80.1, 80.0, True),
            ("gte", 80.0, 80.0, True),
            ("gte", 79.9, 80.0, False),
            ("lt", 11.0, 11.0, False),
            ("lt", 10.9, 11.0, True),
            ("lte", 11.0, 11.0, True),
            ("lte", 11.1, 11.0, False),
        ],
    )
    def test_operators(self, op, value, threshold, expected):
        assert compare(op, value, threshold) is expected

    def test_unknown_operator_never_breaches(self):
        assert compare("eq", 1.0, 1.0) is False
        assert compare("", 1.0, 1.0) is False


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0 s"),
            (45, "45 s"),
            (60, "1 min"),
            (120, "2 min"),
            (150, "2 min 30 s"),
            (3600, "1 h"),
            (3900, "1 h 5 min"),
        ],
    )
    def test_format(self, seconds, expected):
        assert format_duration(seconds) == expected


@pytest.mark.django_db
class TestValidateMetricKey:
    def test_accepts_known_key(self):
        validate_metric_key("cpu.temp_c")  # seeded by 0006

    def test_rejects_unknown_key(self):
        with pytest.raises(ValidationError):
            validate_metric_key("cpu.nonexistent_c")

    def test_accepts_pattern_matching_definition(self):
        validate_metric_key("disk.*.used_pct")

    def test_rejects_pattern_without_match(self):
        with pytest.raises(ValidationError):
            validate_metric_key("nope.*.used_pct")

    def test_rejects_double_wildcard(self):
        with pytest.raises(ValidationError):
            validate_metric_key("disk.*.*")

    @pytest.mark.parametrize("bad", ["", "CPU.Temp", "cpu", "cpu.temp-c", None])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValidationError):
            validate_metric_key(bad)


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestThresholdEngine:
    def test_duration_zero_fires_immediately(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)

        alerts = _check_thresholds()

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.alert_rule == rule
        assert alert.station == online_station
        assert alert.severity == "warning"
        assert alert.title == f"CPU temperature: {online_station.name}"
        assert "85.0°C" in alert.message
        assert "above 80.0°C" in alert.message

    def test_below_threshold_no_alert(self, isolated_rules, online_station):
        _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=50.0)
        assert _check_thresholds() == []
        assert not ThresholdState.objects.exists()

    def test_lt_message_says_below(self, isolated_rules, online_station):
        MetricDefinition.objects.create(
            key="pwr.12v.voltage_v", label="12 V voltage", unit="V", group="power", precision=2
        )
        _rule("pwr.12v.voltage_v", 11.0, op="lt")
        _set_metrics(online_station, pwr__12v__voltage_v=10.5)
        alerts = _check_thresholds()
        assert len(alerts) == 1
        assert "10.50V below 11.00V" in alerts[0].message

    def test_aggregate_selects_field(self, isolated_rules, online_station):
        MetricDefinition.objects.create(key="pwr.12v.voltage_v", label="12V", unit="V")
        _rule("pwr.12v.voltage_v", 11.0, op="lt", aggregate="min")
        StationTelemetry.objects.create(
            station=online_station,
            metrics={
                "pwr.12v.voltage_v": {
                    "min": 10.2,
                    "max": 12.4,
                    "avg": 12.0,
                    "ts": timezone.now().isoformat(),
                }
            },
        )
        assert len(_check_thresholds()) == 1

    def test_non_numeric_value_skipped(self, isolated_rules, online_station):
        _rule("cpu.temp_c", 80)
        StationTelemetry.objects.create(
            station=online_station, metrics={"cpu.temp_c": {"avg": "hot"}}
        )
        assert _check_thresholds() == []

    def test_duration_not_reached_then_reached(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80, duration=60)
        _set_metrics(online_station, cpu__temp_c=85.0)
        t0 = timezone.now()

        assert _check_thresholds(now=t0) == []
        state = ThresholdState.objects.get(rule=rule, station=online_station)
        assert state.breach_since == t0

        _touch(online_station, t0 + timedelta(seconds=30))
        assert _check_thresholds(now=t0 + timedelta(seconds=30)) == []

        _touch(online_station, t0 + timedelta(seconds=60))
        alerts = _check_thresholds(now=t0 + timedelta(seconds=60))
        assert len(alerts) == 1
        assert "for 1 min" in alerts[0].message

    def test_breach_interrupted_restarts_duration(self, isolated_rules, online_station):
        _rule("cpu.temp_c", 80, duration=60)
        t0 = timezone.now()
        _set_metrics(online_station, cpu__temp_c=85.0)
        _check_thresholds(now=t0)
        _set_metrics(online_station, cpu__temp_c=70.0)
        _touch(online_station, t0 + timedelta(seconds=30))
        _check_thresholds(now=t0 + timedelta(seconds=30))
        _set_metrics(online_station, cpu__temp_c=85.0)
        _touch(online_station, t0 + timedelta(seconds=70))
        assert _check_thresholds(now=t0 + timedelta(seconds=70)) == []

    def test_only_one_alert_while_open(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        assert len(_check_thresholds()) == 1
        assert _check_thresholds() == []
        assert _check_thresholds() == []
        assert _open_alerts(rule).count() == 1

    def test_resolve_requires_ok_for_duration_hysteresis(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80, duration=60)
        t0 = timezone.now()
        _set_metrics(online_station, cpu__temp_c=85.0)
        _check_thresholds(now=t0)
        _touch(online_station, t0 + timedelta(seconds=60))
        assert len(_check_thresholds(now=t0 + timedelta(seconds=60))) == 1

        # Back to OK for 30 s — not long enough.
        _set_metrics(online_station, cpu__temp_c=70.0)
        _touch(online_station, t0 + timedelta(seconds=90))
        _check_thresholds(now=t0 + timedelta(seconds=90))
        _touch(online_station, t0 + timedelta(seconds=120))
        _check_thresholds(now=t0 + timedelta(seconds=120))
        assert _open_alerts(rule).count() == 1

        # Breach again: still the same open alert, no second one.
        _set_metrics(online_station, cpu__temp_c=85.0)
        _touch(online_station, t0 + timedelta(seconds=150))
        assert _check_thresholds(now=t0 + timedelta(seconds=150)) == []
        _touch(online_station, t0 + timedelta(seconds=250))
        assert _check_thresholds(now=t0 + timedelta(seconds=250)) == []
        assert Alert.objects.filter(alert_rule=rule).count() == 1
        state = ThresholdState.objects.get(rule=rule)
        assert state.ok_since is None

        # OK for the full duration → resolved.
        _set_metrics(online_station, cpu__temp_c=70.0)
        _touch(online_station, t0 + timedelta(seconds=300))
        _check_thresholds(now=t0 + timedelta(seconds=300))
        assert _open_alerts(rule).count() == 1
        _touch(online_station, t0 + timedelta(seconds=360))
        _check_thresholds(now=t0 + timedelta(seconds=360))
        assert _open_alerts(rule).count() == 0
        alert = Alert.objects.get(alert_rule=rule)
        assert alert.resolved_at is not None
        state.refresh_from_db()
        assert state.alert is None and state.ok_since is None and state.breach_since is None

    def test_duration_zero_resolves_immediately(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        _check_thresholds()
        _set_metrics(online_station, cpu__temp_c=70.0)
        _check_thresholds()
        assert _open_alerts(rule).count() == 0

    def test_offline_station_frozen(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80, duration=60)
        t0 = timezone.now()
        _set_metrics(online_station, cpu__temp_c=85.0)
        _check_thresholds(now=t0)
        _touch(online_station, t0 + timedelta(seconds=60))
        assert len(_check_thresholds(now=t0 + timedelta(seconds=60))) == 1
        before = ThresholdState.objects.values().get(rule=rule)

        # Last heartbeat said OK, then station went silent for 10 min.
        _set_metrics(online_station, cpu__temp_c=70.0)
        later = t0 + timedelta(minutes=11)
        assert _check_thresholds(now=later) == []
        _check_thresholds(now=later + timedelta(minutes=5))
        assert _open_alerts(rule).count() == 1
        assert ThresholdState.objects.values().get(rule=rule) == before

        # Back online, still breached → no duplicate.
        _set_metrics(online_station, cpu__temp_c=85.0)
        _touch(online_station, later + timedelta(minutes=6))
        assert _check_thresholds(now=later + timedelta(minutes=6)) == []
        assert Alert.objects.filter(alert_rule=rule).count() == 1

    def test_never_seen_station_ignored(self, isolated_rules, station):
        _rule("cpu.temp_c", 80)
        _set_metrics(station, cpu__temp_c=85.0)
        assert station.last_seen is None
        assert _check_thresholds() == []

    def test_offline_station_does_not_start_breach(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        assert _check_thresholds(now=timezone.now() + timedelta(minutes=10)) == []
        assert not ThresholdState.objects.filter(rule=rule).exists()

    def test_missing_key_frozen(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80, duration=60)
        t0 = timezone.now()
        _set_metrics(online_station, cpu__temp_c=85.0)
        _check_thresholds(now=t0)
        _touch(online_station, t0 + timedelta(seconds=60))
        _check_thresholds(now=t0 + timedelta(seconds=60))
        assert _open_alerts(rule).count() == 1
        before = ThresholdState.objects.values().get(rule=rule)

        _set_metrics(online_station, mem__used_pct=10.0)  # cpu.temp_c gone
        for s in (120, 240, 600):
            _touch(online_station, t0 + timedelta(seconds=s))
            assert _check_thresholds(now=t0 + timedelta(seconds=s)) == []
        assert _open_alerts(rule).count() == 1
        assert ThresholdState.objects.values().get(rule=rule) == before

    def test_stale_sample_ts_frozen(self, isolated_rules, online_station):
        """Station online (last_seen fresh) but the sample is old -> no current value."""
        rule = _rule("cpu.temp_c", 80)
        stale = timezone.now() - engine.OFFLINE_THRESHOLD - timedelta(minutes=1)
        StationTelemetry.objects.create(
            station=online_station, metrics={"cpu.temp_c": _val(85.0, stale)}
        )
        assert _check_thresholds() == []
        assert not ThresholdState.objects.filter(rule=rule).exists()

    @pytest.mark.parametrize("ts", [None, "", "x", 12345])
    def test_missing_or_bad_ts_frozen(self, isolated_rules, online_station, ts):
        rule = _rule("cpu.temp_c", 80)
        sample = {"min": 85.0, "max": 85.0, "avg": 85.0}
        if ts is not None:
            sample["ts"] = ts
        StationTelemetry.objects.create(station=online_station, metrics={"cpu.temp_c": sample})
        assert _check_thresholds() == []
        assert not ThresholdState.objects.filter(rule=rule).exists()

    def test_naive_ts_treated_as_utc(self, isolated_rules, online_station):
        _rule("cpu.temp_c", 80)
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        sample = {"min": 85.0, "max": 85.0, "avg": 85.0, "ts": naive}
        StationTelemetry.objects.create(station=online_station, metrics={"cpu.temp_c": sample})
        assert len(_check_thresholds()) == 1

    def test_open_alert_not_resolved_by_stale_ok_value(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        assert len(_check_thresholds()) == 1
        before = ThresholdState.objects.values().get(rule=rule)

        stale = timezone.now() - engine.OFFLINE_THRESHOLD - timedelta(minutes=1)
        StationTelemetry.objects.filter(station=online_station).update(
            metrics={"cpu.temp_c": _val(70.0, stale)}
        )
        assert _check_thresholds() == []
        assert _open_alerts(rule).count() == 1
        assert ThresholdState.objects.values().get(rule=rule) == before

    def test_pattern_rule_two_disks(self, isolated_rules, online_station):
        rule = _rule("disk.*.used_pct", 90)
        _set_metrics(online_station, disk__root__used_pct=92.0, disk__boot__used_pct=97.0)
        alerts = _check_thresholds()
        assert len(alerts) == 2
        keys = set(ThresholdState.objects.filter(rule=rule).values_list("metric_key", flat=True))
        assert keys == {"disk.root.used_pct", "disk.boot.used_pct"}
        titles = {a.title for a in alerts}
        assert titles == {
            f"Disk used /: {online_station.name}",
            f"Disk used /boot: {online_station.name}",
        }

        # Only root recovers → only its alert resolves.
        _set_metrics(online_station, disk__root__used_pct=50.0, disk__boot__used_pct=97.0)
        _check_thresholds()
        assert _open_alerts(rule).count() == 1
        assert "/boot" in _open_alerts(rule).get().title

    def test_warning_and_critical_both_open(self, isolated_rules, online_station):
        warn = _rule("disk.*.used_pct", 90, severity="warning")
        crit = _rule("disk.*.used_pct", 95, severity="critical")
        _set_metrics(online_station, disk__root__used_pct=97.0)
        alerts = _check_thresholds()
        assert len(alerts) == 2
        assert _open_alerts(warn).get().severity == "warning"
        assert _open_alerts(crit).get().severity == "critical"

    def test_state_survives_restart(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80, duration=60)
        t0 = timezone.now()
        _set_metrics(online_station, cpu__temp_c=85.0)
        _check_thresholds(now=t0)
        # "Restart": no in-memory state, everything comes from the DB.
        engine._unresolved_cache = None
        _touch(online_station, t0 + timedelta(seconds=61))
        alerts = _check_thresholds(now=t0 + timedelta(seconds=61))
        assert len(alerts) == 1
        assert ThresholdState.objects.get(rule=rule).alert == alerts[0]

    def test_blocked_definition_ignored(self, isolated_rules, online_station):
        _rule("cpu.temp_c", 80)
        MetricDefinition.objects.filter(key="cpu.temp_c").update(is_blocked=True)
        _set_metrics(online_station, cpu__temp_c=85.0)
        assert _check_thresholds() == []

    def test_key_without_definition_ignored(self, isolated_rules, online_station):
        MetricDefinition.objects.filter(key="disk.boot.used_pct").delete()
        _rule("disk.*.used_pct", 90)
        _set_metrics(online_station, disk__root__used_pct=92.0, disk__boot__used_pct=99.0)
        alerts = _check_thresholds()
        assert len(alerts) == 1
        assert "Disk used /:" in alerts[0].title

    def test_inactive_rule_ignored(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        AlertRule.objects.filter(pk=rule.pk).update(is_active=False)
        _set_metrics(online_station, cpu__temp_c=85.0)
        assert _check_thresholds() == []

    def test_manually_resolved_alert_refires_while_breached(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        first = _check_thresholds()[0]
        Alert.objects.filter(pk=first.pk).update(is_resolved=True, resolved_at=timezone.now())
        second = _check_thresholds()
        assert len(second) == 1 and second[0].pk != first.pk
        assert ThresholdState.objects.get(rule=rule).alert_id == second[0].pk

    def test_seeded_cpu_rule_fixture(self, isolated_rules, cpu_temp_alert_rule, online_station):
        """Re-covers the legacy TestCheckCpuTemperature via the generic engine."""
        _set_metrics(online_station, cpu__temp_c=85.0)
        alerts = _check_thresholds()
        assert len(alerts) == 1
        assert "temperature" in alerts[0].title.lower()

    def test_query_count_bounded(
        self, isolated_rules, station_factory, django_assert_max_num_queries
    ):
        """Loads are per run, not per station × rule (no N+1).

        4 loads (rules, definitions, telemetry, states) + per rule a constant
        savepoint / row lock / release — independent of the station count."""
        now = timezone.now()
        rules = [_rule(key, 99) for key in ("cpu.temp_c", "mem.used_pct", "disk.*.used_pct")]
        for n_stations in (5, 10):
            while len(StationTelemetry.objects.all()) < n_stations:
                st = station_factory(last_seen=now)
                _set_metrics(st, cpu__temp_c=50.0, mem__used_pct=10.0, disk__root__used_pct=10.0)
            with django_assert_max_num_queries(4 + 3 * len(rules)):
                assert _check_thresholds(now=now) == []


# ---------------------------------------------------------------------------
# rule change / delete reset (Review Focus 4)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRuleChangeReset:
    def _fire(self, station, rule):
        _set_metrics(station, cpu__temp_c=85.0)
        alerts = _check_thresholds()
        assert len(alerts) == 1
        return alerts[0]

    def test_reset_rule_evaluation(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        alert = self._fire(online_station, rule)
        assert reset_rule_evaluation(rule) == 1
        alert.refresh_from_db()
        assert alert.is_resolved and alert.resolved_at is not None
        assert not ThresholdState.objects.filter(rule=rule).exists()

    def test_save_with_change_resolves_and_resets(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        alert = self._fire(online_station, rule)
        rule.threshold = 90
        rule.save()
        alert.refresh_from_db()
        assert alert.is_resolved
        assert not ThresholdState.objects.filter(rule=rule).exists()
        # Fresh evaluation with the new values: 85 < 90 → nothing.
        assert _check_thresholds() == []

    def test_save_update_fields_change_resets(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        alert = self._fire(online_station, rule)
        rule.is_active = False
        rule.save(update_fields=["is_active"])
        alert.refresh_from_db()
        assert alert.is_resolved

    def test_save_then_refires_with_new_values(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        old = self._fire(online_station, rule)
        rule.threshold = 82
        rule.save()
        new = _check_thresholds()
        assert len(new) == 1 and new[0].pk != old.pk

    def test_noop_save_keeps_alert(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        alert = self._fire(online_station, rule)
        rule.description = "cosmetic change only"
        rule.name = "renamed"
        rule.save()
        alert.refresh_from_db()
        assert not alert.is_resolved
        assert ThresholdState.objects.filter(rule=rule).exists()

    def test_delete_resolves_alert(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        alert = self._fire(online_station, rule)
        rule.delete()
        alert.refresh_from_db()
        assert alert.is_resolved
        assert alert.alert_rule is None
        assert not ThresholdState.objects.exists()

    def test_event_rule_save_untouched(self, offline_alert_rule, station):
        alert = Alert.objects.create(
            station=station,
            alert_rule=offline_alert_rule,
            severity="critical",
            title="Station offline",
            message="x",
        )
        offline_alert_rule.threshold = 5
        offline_alert_rule.save()
        alert.refresh_from_db()
        assert not alert.is_resolved

    def test_create_does_not_fire_signal(self, isolated_rules, online_station):
        # Creating a second rule must not touch alerts of another rule.
        rule = _rule("cpu.temp_c", 80)
        alert = self._fire(online_station, rule)
        _rule("cpu.temp_c", 90)
        alert.refresh_from_db()
        assert not alert.is_resolved


# ---------------------------------------------------------------------------
# review round 2: gaps restart pending timers (A), blocking resets (B),
# engine/rule-mutation race (F)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGapRestartsPendingTimers:
    def test_pending_breach_restarts_after_gap(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80, duration=60)
        t0 = timezone.now()
        _set_metrics(online_station, cpu__temp_c=85.0)
        assert _check_thresholds(now=t0) == []
        state = ThresholdState.objects.get(rule=rule)
        assert state.breach_since == t0 and state.last_evaluated_at == t0

        # 20 min silence, then a breached sample: the gap must not count.
        later = t0 + timedelta(minutes=20)
        _touch(online_station, later)
        assert _check_thresholds(now=later) == []
        state.refresh_from_db()
        assert state.breach_since == later
        assert state.last_evaluated_at == later

        _touch(online_station, later + timedelta(seconds=30))
        assert _check_thresholds(now=later + timedelta(seconds=30)) == []
        _touch(online_station, later + timedelta(seconds=60))
        assert len(_check_thresholds(now=later + timedelta(seconds=60))) == 1

    def test_pending_resolve_restarts_after_gap(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80, duration=60)
        t0 = timezone.now()
        _set_metrics(online_station, cpu__temp_c=85.0)
        _check_thresholds(now=t0)
        _touch(online_station, t0 + timedelta(seconds=60))
        assert len(_check_thresholds(now=t0 + timedelta(seconds=60))) == 1

        _set_metrics(online_station, cpu__temp_c=70.0)
        t1 = t0 + timedelta(seconds=90)
        _touch(online_station, t1)
        _check_thresholds(now=t1)
        assert ThresholdState.objects.get(rule=rule).ok_since == t1

        later = t1 + timedelta(minutes=20)
        _touch(online_station, later)
        _check_thresholds(now=later)
        assert _open_alerts(rule).count() == 1
        assert ThresholdState.objects.get(rule=rule).ok_since == later

        _touch(online_station, later + timedelta(seconds=60))
        _check_thresholds(now=later + timedelta(seconds=60))
        assert _open_alerts(rule).count() == 0

    def test_short_interval_is_not_a_gap(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80, duration=600)
        t0 = timezone.now()
        _set_metrics(online_station, cpu__temp_c=85.0)
        _check_thresholds(now=t0)
        for minutes in (4, 8):
            _touch(online_station, t0 + timedelta(minutes=minutes))
            _check_thresholds(now=t0 + timedelta(minutes=minutes))
        assert ThresholdState.objects.get(rule=rule).breach_since == t0
        _touch(online_station, t0 + timedelta(minutes=10))
        assert len(_check_thresholds(now=t0 + timedelta(minutes=10))) == 1


@pytest.mark.django_db
class TestBlockingMetricResets:
    def test_block_resolves_alerts_and_deletes_states(self, isolated_rules, online_station):
        rule = _rule("disk.*.used_pct", 90)
        _set_metrics(online_station, disk__root__used_pct=95.0, disk__boot__used_pct=95.0)
        assert len(_check_thresholds()) == 2

        d = MetricDefinition.objects.get(key="disk.boot.used_pct")
        d.is_blocked = True
        d.save()

        open_titles = list(_open_alerts(rule).values_list("title", flat=True))
        assert len(open_titles) == 1 and "/boot" not in open_titles[0]
        keys = set(ThresholdState.objects.filter(rule=rule).values_list("metric_key", flat=True))
        assert keys == {"disk.root.used_pct"}

    def test_unrelated_save_keeps_alert(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        assert len(_check_thresholds()) == 1
        d = MetricDefinition.objects.get(key="cpu.temp_c")
        d.label_override = "CPU"
        d.save()
        assert _open_alerts(rule).count() == 1
        assert ThresholdState.objects.filter(rule=rule).exists()


@pytest.mark.django_db
class TestRuleLockRace:
    def test_rule_deactivated_after_load_is_skipped(
        self, isolated_rules, online_station, monkeypatch
    ):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        real = engine.MetricDefinition.objects.filter

        def deactivate_then_filter(*args, **kwargs):
            # Runs after the rules were loaded: a concurrent disable.
            AlertRule.objects.filter(pk=rule.pk).update(is_active=False)
            return real(*args, **kwargs)

        monkeypatch.setattr(engine.MetricDefinition.objects, "filter", deactivate_then_filter)
        assert _check_thresholds() == []
        assert not Alert.objects.filter(alert_rule=rule).exists()
        assert not ThresholdState.objects.filter(rule=rule).exists()

    def test_engine_uses_fresh_rule_fields(self, isolated_rules, online_station, monkeypatch):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        real = engine.MetricDefinition.objects.filter

        def raise_threshold_then_filter(*args, **kwargs):
            AlertRule.objects.filter(pk=rule.pk).update(threshold=90)
            return real(*args, **kwargs)

        monkeypatch.setattr(engine.MetricDefinition.objects, "filter", raise_threshold_then_filter)
        assert _check_thresholds() == []
        assert not Alert.objects.filter(alert_rule=rule).exists()

    def test_reset_resolves_alert_created_right_before(self, isolated_rules, online_station):
        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        alert = _check_thresholds()[0]
        rule.is_active = False
        rule.save()
        alert.refresh_from_db()
        assert alert.is_resolved
        assert not ThresholdState.objects.filter(rule=rule).exists()

    @pytest.mark.parametrize("fn", ["reset", "resolve"])
    def test_reset_takes_rule_row_lock_first(
        self, isolated_rules, online_station, fn, monkeypatch
    ):
        from apps.monitoring import rules as rules_mod

        rule = _rule("cpu.temp_c", 80)
        _set_metrics(online_station, cpu__temp_c=85.0)
        alert = _check_thresholds()[0]
        calls = []
        real = AlertRule.objects.select_for_update

        def spy(*args, **kwargs):
            # The alert must still be open when the lock is requested.
            alert.refresh_from_db()
            calls.append(alert.is_resolved)
            return real(*args, **kwargs)

        monkeypatch.setattr(AlertRule.objects, "select_for_update", spy)
        target = (
            rules_mod.reset_rule_evaluation if fn == "reset" else rules_mod.resolve_open_alerts
        )
        assert target(rule) == 1
        assert calls == [False]
