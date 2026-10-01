"""Tests for new telemetry alert types and default rules (Task 12)."""

import pytest
from django.core.management import call_command

from apps.monitoring.models import AlertRule


def test_new_alert_types_exist():
    assert AlertRule.AlertType.UNEXPECTED_REBOOT == "unexpected_reboot"
    assert AlertRule.AlertType.POWER_WARNING == "power_warning"
    assert AlertRule.AlertType.STORAGE_HEALTH == "storage_health"


@pytest.mark.django_db
def test_default_rules_seed_new_types():
    call_command("create_default_alert_rules")
    for t in (
        AlertRule.AlertType.UNEXPECTED_REBOOT,
        AlertRule.AlertType.POWER_WARNING,
        AlertRule.AlertType.STORAGE_HEALTH,
    ):
        assert AlertRule.objects.filter(alert_type=t).exists()
