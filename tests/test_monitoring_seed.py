"""Verify the migrations seed the default AlertRule set.

Event rules (one per alert_type) are seeded by 0002/0004; the legacy
CPU/Disk/RAM rules are converted to generic threshold rules and the 12 V
undervoltage rule is added by 0006_generic_metrics_data. Without these rows
check_alerts() silently no-ops, so a refactor that drops or skips a seed
migration must fail CI loudly.
"""

import pytest
from django.core.management import call_command

from apps.monitoring.models import AlertRule

EXPECTED_EVENT_RULES = {
    AlertRule.AlertType.STATION_OFFLINE: {
        "threshold": 5.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
    AlertRule.AlertType.OTA_FAILED: {"threshold": 0.0, "severity": AlertRule.Severity.CRITICAL},
    AlertRule.AlertType.UNEXPECTED_REBOOT: {
        "threshold": 0.0,
        "severity": AlertRule.Severity.WARNING,
    },
    AlertRule.AlertType.POWER_WARNING: {"threshold": 0.0, "severity": AlertRule.Severity.WARNING},
    AlertRule.AlertType.STORAGE_HEALTH: {
        "threshold": 80.0,
        "severity": AlertRule.Severity.WARNING,
    },
}

# (name, metric_key, operator, threshold, aggregate, duration_seconds, severity)
EXPECTED_THRESHOLD_RULES = [
    ("CPU temperature", "cpu.temp_c", "gte", 80.0, "avg", 0, AlertRule.Severity.WARNING),
    ("Disk usage (warning)", "disk.*.used_pct", "gte", 90.0, "avg", 0, AlertRule.Severity.WARNING),
    (
        "Disk usage (critical)",
        "disk.*.used_pct",
        "gte",
        95.0,
        "avg",
        0,
        AlertRule.Severity.CRITICAL,
    ),
    ("Memory usage", "mem.used_pct", "gte", 90.0, "avg", 0, AlertRule.Severity.CRITICAL),
    (
        "12 V rail undervoltage",
        "pwr.12v.voltage_v",
        "lt",
        11.0,
        "avg",
        60,
        AlertRule.Severity.WARNING,
    ),
]


@pytest.mark.django_db
def test_event_rules_seeded_with_defaults():
    for alert_type, expected in EXPECTED_EVENT_RULES.items():
        rule = AlertRule.objects.get(alert_type=alert_type)
        assert rule.threshold == expected["threshold"], alert_type
        assert rule.severity == expected["severity"], alert_type
        assert rule.is_active is True, alert_type


@pytest.mark.django_db
def test_no_legacy_alert_types_left():
    legacy = {"cpu_temperature", "disk_warning", "disk_critical", "ram_critical"}
    assert not AlertRule.objects.filter(alert_type__in=legacy).exists()


@pytest.mark.django_db
def test_threshold_rules_seeded_with_defaults():
    for name, key, op, threshold, aggregate, duration, severity in EXPECTED_THRESHOLD_RULES:
        rule = AlertRule.objects.get(
            alert_type="threshold", metric_key=key, operator=op, name=name
        )
        assert rule.threshold == threshold, name
        assert rule.aggregate == aggregate, name
        assert rule.duration_seconds == duration, name
        assert rule.severity == severity, name
        assert rule.is_active is True, name


@pytest.mark.django_db
def test_management_command_is_idempotent_and_restores_deleted():
    before = AlertRule.objects.count()
    call_command("create_default_alert_rules", stdout=None)
    assert AlertRule.objects.count() == before

    AlertRule.objects.filter(alert_type="threshold", metric_key="disk.*.used_pct").delete()
    AlertRule.objects.filter(alert_type="ota_failed").delete()
    call_command("create_default_alert_rules")
    assert AlertRule.objects.count() == before
    assert (
        AlertRule.objects.filter(alert_type="threshold", metric_key="disk.*.used_pct").count() == 2
    )


@pytest.mark.django_db
def test_management_command_does_not_duplicate_renamed_rule():
    AlertRule.objects.filter(alert_type="threshold", metric_key="cpu.temp_c").update(
        name="Mein CPU-Alarm"
    )
    AlertRule.objects.filter(
        alert_type="threshold", metric_key="disk.*.used_pct", severity="critical"
    ).update(name="Platte voll")
    before = AlertRule.objects.count()
    call_command("create_default_alert_rules")
    assert AlertRule.objects.count() == before
    assert AlertRule.objects.filter(alert_type="threshold", metric_key="cpu.temp_c").count() == 1


@pytest.mark.django_db
def test_management_command_tolerates_multiple_matching_rules():
    for _ in range(2):
        AlertRule.objects.create(
            alert_type="threshold",
            name="CPU temperature",
            metric_key="cpu.temp_c",
            operator="gte",
            threshold=70.0,
        )
        AlertRule.objects.create(
            alert_type="threshold",
            name="Disk usage (warning)",
            metric_key="disk.*.used_pct",
            operator="gte",
            threshold=80.0,
            severity="warning",
        )
    before = AlertRule.objects.count()
    call_command("create_default_alert_rules")
    assert AlertRule.objects.count() == before


@pytest.mark.django_db
def test_management_command_restores_only_missing_disk_severity():
    AlertRule.objects.filter(
        alert_type="threshold", metric_key="disk.*.used_pct", severity="critical"
    ).delete()
    call_command("create_default_alert_rules")
    disk = AlertRule.objects.filter(alert_type="threshold", metric_key="disk.*.used_pct")
    assert sorted(disk.values_list("severity", flat=True)) == ["critical", "warning"]


@pytest.mark.django_db
def test_management_command_no_extra_disk_rule_after_severity_change():
    AlertRule.objects.filter(
        alert_type="threshold", metric_key="disk.*.used_pct", severity="warning"
    ).update(severity="critical")
    before = AlertRule.objects.count()
    call_command("create_default_alert_rules")
    assert AlertRule.objects.count() == before
    assert (
        AlertRule.objects.filter(alert_type="threshold", metric_key="disk.*.used_pct").count() == 2
    )


@pytest.mark.django_db
def test_management_command_no_extra_cpu_rule_after_operator_change():
    AlertRule.objects.filter(alert_type="threshold", metric_key="cpu.temp_c").update(operator="gt")
    before = AlertRule.objects.count()
    call_command("create_default_alert_rules")
    assert AlertRule.objects.count() == before
    assert AlertRule.objects.filter(alert_type="threshold", metric_key="cpu.temp_c").count() == 1


@pytest.mark.django_db
def test_management_command_one_disk_left_with_changed_severity_restores_one():
    disk = AlertRule.objects.filter(alert_type="threshold", metric_key="disk.*.used_pct")
    disk.filter(severity="critical").delete()
    disk.update(operator="gt")
    call_command("create_default_alert_rules")
    # One rule exists for a group of two and matches neither default: at most
    # one default is created, never both.
    assert disk.count() == 2
