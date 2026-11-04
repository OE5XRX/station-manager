"""Verify monitoring.0006_generic_metrics_data converts legacy rules (spec §5.3).

Starts at ``monitoring 0004`` (legacy typed CPU/Disk/RAM rules), seeds
operator-tuned values + an open alert, then applies the schema + data
migrations.
"""

import importlib

import pytest

PRE = [
    ("monitoring", "0004_seed_telemetry_alert_rules"),
    ("stations", "0029_alter_station_rollout_group"),
]
TESTED = [
    ("monitoring", "0006_generic_metrics_data"),
    ("stations", "0031_stationtelemetry_metrics"),
]

LEGACY = {
    "cpu_temperature": ("cpu.temp_c", "CPU temperature"),
    "disk_warning": ("disk.*.used_pct", "Disk usage (warning)"),
    "disk_critical": ("disk.*.used_pct", "Disk usage (critical)"),
    "ram_critical": ("mem.used_pct", "Memory usage"),
}


def _seed(apps):
    AlertRule = apps.get_model("monitoring", "AlertRule")
    Alert = apps.get_model("monitoring", "Alert")
    Station = apps.get_model("stations", "Station")
    defaults = {
        "cpu_temperature": (80.0, "warning"),
        "disk_warning": (90.0, "warning"),
        "disk_critical": (95.0, "critical"),
        "ram_critical": (90.0, "critical"),
    }
    rules = {}
    for alert_type, (threshold, severity) in defaults.items():
        rules[alert_type], _ = AlertRule.objects.update_or_create(
            alert_type=alert_type,
            defaults={
                "threshold": threshold,
                "severity": severity,
                "is_active": True,
                "description": "d",
            },
        )
    # Operator-tuned CPU rule.
    cpu = rules["cpu_temperature"]
    cpu.threshold = 75.0
    cpu.is_active = False
    cpu.severity = "critical"
    cpu.description = "tuned"
    cpu.save()
    station = Station.objects.create(name="mig-station")
    alert = Alert.objects.create(
        station=station, alert_rule=cpu, severity="critical", title="hot", message="m"
    )
    offline = AlertRule.objects.filter(alert_type="station_offline").first()
    event_alert = None
    if offline is not None:
        event_alert = Alert.objects.create(
            station=station, alert_rule=offline, severity="critical", title="off", message="m"
        )
    return {k: r.pk for k, r in rules.items()}, alert.pk, event_alert.pk if event_alert else None


@pytest.fixture
def migrated(migrator):
    old = migrator.apply_initial_migration(PRE)
    rule_pks, alert_pk, event_alert_pk = _seed(old.apps)
    new = migrator.apply_tested_migration(TESTED)
    return new.apps, rule_pks, alert_pk, event_alert_pk


def test_legacy_rules_converted_in_place(migrated):
    apps, rule_pks, _, _ = migrated
    AlertRule = apps.get_model("monitoring", "AlertRule")
    for alert_type, (metric_key, name) in LEGACY.items():
        rule = AlertRule.objects.get(pk=rule_pks[alert_type])
        assert rule.alert_type == "threshold"
        assert rule.metric_key == metric_key
        assert rule.name == name
        assert rule.operator == "gte"
        assert rule.aggregate == "avg"
        assert rule.duration_seconds == 0
    assert not AlertRule.objects.filter(alert_type__in=list(LEGACY)).exists()


def test_operator_tuned_values_kept(migrated):
    apps, rule_pks, _, _ = migrated
    rule = apps.get_model("monitoring", "AlertRule").objects.get(pk=rule_pks["cpu_temperature"])
    assert rule.threshold == 75.0
    assert rule.is_active is False
    assert rule.severity == "critical"
    assert rule.description == "tuned"
    disk_crit = apps.get_model("monitoring", "AlertRule").objects.get(pk=rule_pks["disk_critical"])
    assert disk_crit.threshold == 95.0


def test_open_legacy_alert_keeps_fk_and_is_resolved(migrated):
    apps, rule_pks, alert_pk, event_alert_pk = migrated
    Alert = apps.get_model("monitoring", "Alert")
    alert = Alert.objects.get(pk=alert_pk)
    assert alert.alert_rule_id == rule_pks["cpu_temperature"]
    assert alert.is_resolved is True
    assert alert.resolved_at is not None
    if event_alert_pk is not None:
        assert Alert.objects.get(pk=event_alert_pk).is_resolved is False


def test_legacy_definitions_created(migrated):
    apps, *_ = migrated
    MetricDefinition = apps.get_model("monitoring", "MetricDefinition")
    expected = {
        "cpu.temp_c": ("CPU temperature", "°C", "system", 1, -40.0, 125.0),
        "mem.used_pct": ("Memory used", "%", "system", 1, 0.0, 100.0),
        "disk.root.used_pct": ("Disk used /", "%", "disk", 1, 0.0, 100.0),
        "disk.boot.used_pct": ("Disk used /boot", "%", "disk", 1, 0.0, 100.0),
        "sys.uptime_s": ("Uptime", "s", "system", 0, 0.0, None),
    }
    for key, (label, unit, group, precision, lo, hi) in expected.items():
        d = MetricDefinition.objects.get(key=key)
        assert (d.label, d.unit, d.group, d.precision, d.min_value, d.max_value) == (
            label,
            unit,
            group,
            precision,
            lo,
            hi,
        )


def test_12v_rule_created_once_and_rerun_idempotent(migrated):
    apps, *_ = migrated
    AlertRule = apps.get_model("monitoring", "AlertRule")
    MetricDefinition = apps.get_model("monitoring", "MetricDefinition")
    qs = AlertRule.objects.filter(
        alert_type="threshold", metric_key="pwr.12v.voltage_v", operator="lt"
    )
    assert qs.count() == 1
    rule = qs.get()
    assert (rule.threshold, rule.aggregate, rule.duration_seconds, rule.severity) == (
        11.0,
        "avg",
        60,
        "warning",
    )
    assert rule.name == "12 V rail undervoltage"

    mod = importlib.import_module("apps.monitoring.migrations.0006_generic_metrics_data")
    n_rules = AlertRule.objects.count()
    n_defs = MetricDefinition.objects.count()
    mod.forwards(apps, None)
    assert qs.count() == 1
    assert AlertRule.objects.count() == n_rules
    assert MetricDefinition.objects.count() == n_defs


def test_existing_definition_not_overwritten(migrator):
    """Definitions are get_or_create — a pre-existing row keeps its values on re-run."""
    migrator.apply_initial_migration(PRE)
    new = migrator.apply_tested_migration(TESTED)
    MetricDefinition = new.apps.get_model("monitoring", "MetricDefinition")
    MetricDefinition.objects.filter(key="cpu.temp_c").update(
        label="Agent label", label_override="X"
    )
    mod = importlib.import_module("apps.monitoring.migrations.0006_generic_metrics_data")
    mod.forwards(new.apps, None)
    d = MetricDefinition.objects.get(key="cpu.temp_c")
    assert (d.label, d.label_override) == ("Agent label", "X")


def test_reverse_to_0004_restores_legacy_rules(migrator):
    """Rolling 0006/0005 back must leave exactly one rule per legacy type
    (0004 has alert_type unique) with tuned values kept."""
    new = migrator.apply_initial_migration(TESTED)
    AlertRule = new.apps.get_model("monitoring", "AlertRule")
    Alert = new.apps.get_model("monitoring", "Alert")
    ThresholdState = new.apps.get_model("monitoring", "ThresholdState")
    Station = new.apps.get_model("stations", "Station")
    station = Station.objects.create(name="rev-station")

    cpu = AlertRule.objects.get(alert_type="threshold", metric_key="cpu.temp_c")
    cpu.threshold = 77.0
    cpu.save()
    # Operator-created extra rules: a second CPU rule and a custom metric rule.
    AlertRule.objects.create(
        alert_type="threshold", metric_key="cpu.temp_c", operator="gte", threshold=60.0
    )
    custom = AlertRule.objects.create(
        alert_type="threshold", metric_key="net.eth0.rx_kbps", operator="gt", threshold=1.0
    )
    alert = Alert.objects.create(
        station=station, alert_rule=custom, severity="warning", title="t", message="m"
    )
    ThresholdState.objects.create(
        rule=custom, station=station, metric_key="net.eth0.rx_kbps", alert=alert
    )

    old = migrator.apply_tested_migration(PRE)
    AlertRule = old.apps.get_model("monitoring", "AlertRule")
    Alert = old.apps.get_model("monitoring", "Alert")
    for legacy in ("cpu_temperature", "disk_warning", "disk_critical", "ram_critical"):
        assert AlertRule.objects.filter(alert_type=legacy).count() == 1
    assert not AlertRule.objects.filter(alert_type="threshold").exists()
    assert AlertRule.objects.get(alert_type="cpu_temperature").threshold == 77.0
    assert AlertRule.objects.get(alert_type="disk_warning").threshold == 90.0
    assert AlertRule.objects.get(alert_type="disk_critical").threshold == 95.0
    assert AlertRule.objects.get(alert_type="disk_critical").severity == "critical"
    # Alerts of deleted rules survive with the FK nulled.
    kept = Alert.objects.get(pk=alert.pk)
    assert kept.alert_rule_id is None
