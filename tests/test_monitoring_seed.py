"""Verify the data migration seeds the system-managed AlertRule set.

These rules are not user-creatable (alert_type is unique + restricted to a
TextChoices enum, engine.py has one hard-coded check per type). Without the
seed migration, every check_alerts() tick silently no-ops because
_get_active_rule() returns None for every type. This test exists so a
future refactor that drops or skips the migration fails CI loudly.
"""

import pytest

from apps.monitoring.models import AlertRule

EXPECTED_RULES = {
    AlertRule.AlertType.STATION_OFFLINE: {
        "threshold": 5.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
    AlertRule.AlertType.CPU_TEMPERATURE: {
        "threshold": 80.0,
        "severity": AlertRule.Severity.WARNING,
    },
    AlertRule.AlertType.DISK_WARNING: {
        "threshold": 90.0,
        "severity": AlertRule.Severity.WARNING,
    },
    AlertRule.AlertType.DISK_CRITICAL: {
        "threshold": 95.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
    AlertRule.AlertType.RAM_CRITICAL: {
        "threshold": 90.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
    AlertRule.AlertType.OTA_FAILED: {
        "threshold": 0.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
}


@pytest.mark.django_db
def test_all_default_rules_exist():
    """Every alert_type in EXPECTED_RULES has a row in the DB."""
    actual_types = set(AlertRule.objects.values_list("alert_type", flat=True))
    expected_types = set(EXPECTED_RULES.keys())
    assert actual_types >= expected_types, f"Missing seeded rules: {expected_types - actual_types}"


@pytest.mark.django_db
def test_default_rules_have_expected_threshold_and_severity():
    """Seeded rules carry the documented threshold + severity defaults."""
    for alert_type, expected in EXPECTED_RULES.items():
        rule = AlertRule.objects.get(alert_type=alert_type)
        assert rule.threshold == expected["threshold"], (
            f"{alert_type}: threshold {rule.threshold} != {expected['threshold']}"
        )
        assert rule.severity == expected["severity"], (
            f"{alert_type}: severity {rule.severity} != {expected['severity']}"
        )


@pytest.mark.django_db
def test_default_rules_are_active():
    """Seeded rules are is_active=True so the engine picks them up."""
    for alert_type in EXPECTED_RULES.keys():
        rule = AlertRule.objects.get(alert_type=alert_type)
        assert rule.is_active is True, f"{alert_type}: is_active=False"
