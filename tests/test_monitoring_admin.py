"""Django admin for monitoring: threshold-rule + metric-definition validation."""

import pytest
from django.urls import reverse

from apps.monitoring.models import AlertRule, MetricDefinition

pytestmark = pytest.mark.django_db


def _rule_post(**overrides):
    data = {
        "alert_type": "threshold",
        "name": "Admin rule",
        "metric_key": "adm.value_pct",
        "operator": "gte",
        "aggregate": "avg",
        "threshold": "50",
        "duration_seconds": "0",
        "severity": "warning",
        "is_active": "on",
        "description": "",
    }
    data.update(overrides)
    return data


@pytest.fixture
def admin_client_logged(client, admin_user):
    client.force_login(admin_user)
    return client


def test_admin_threshold_rule_requires_operator(admin_client_logged):
    MetricDefinition.objects.create(key="adm.value_pct")
    r = admin_client_logged.post(
        reverse("admin:monitoring_alertrule_add"), _rule_post(operator="")
    )
    assert r.status_code == 200
    assert "operator" in r.context["adminform"].form.errors
    assert not AlertRule.objects.filter(name="Admin rule").exists()


def test_admin_threshold_rule_unknown_key_rejected(admin_client_logged):
    r = admin_client_logged.post(
        reverse("admin:monitoring_alertrule_add"), _rule_post(metric_key="no.such_key")
    )
    assert r.status_code == 200
    assert "metric_key" in r.context["adminform"].form.errors
    assert not AlertRule.objects.filter(name="Admin rule").exists()


def test_admin_threshold_rule_known_key_created(admin_client_logged):
    MetricDefinition.objects.create(key="adm.value_pct")
    r = admin_client_logged.post(reverse("admin:monitoring_alertrule_add"), _rule_post())
    assert r.status_code == 302
    assert AlertRule.objects.filter(name="Admin rule", metric_key="adm.value_pct").exists()


def test_admin_threshold_rule_unchanged_undescribed_key_allowed(admin_client_logged):
    rule = AlertRule.objects.create(
        alert_type="threshold",
        name="Seeded",
        metric_key="seeded.undescribed_v",
        operator="lt",
        threshold=11.0,
        severity="warning",
    )
    r = admin_client_logged.post(
        reverse("admin:monitoring_alertrule_change", args=[rule.pk]),
        _rule_post(
            name="Seeded", metric_key="seeded.undescribed_v", operator="lt", threshold="10.5"
        ),
    )
    assert r.status_code == 302
    rule.refresh_from_db()
    assert rule.threshold == 10.5


def test_admin_event_rule_untouched(admin_client_logged):
    AlertRule.objects.filter(alert_type="unexpected_reboot").delete()
    r = admin_client_logged.post(
        reverse("admin:monitoring_alertrule_add"),
        _rule_post(alert_type="unexpected_reboot", metric_key="", operator="", threshold="0"),
    )
    assert r.status_code == 302
    assert AlertRule.objects.filter(alert_type="unexpected_reboot").exists()


def test_admin_metric_definition_inverted_effective_range_rejected(admin_client_logged):
    d = MetricDefinition.objects.create(key="pwr.12v.voltage_v", min_value=0.0, max_value=30.0)
    r = admin_client_logged.post(
        reverse("admin:monitoring_metricdefinition_change", args=[d.pk]),
        {"label_override": "", "min_override": "40", "max_override": ""},
    )
    assert r.status_code == 200
    assert "min_override" in r.context["adminform"].form.errors
    d.refresh_from_db()
    assert d.min_override is None


def test_admin_metric_definition_valid_override_saved(admin_client_logged):
    d = MetricDefinition.objects.create(key="pwr.12v.voltage_v", min_value=0.0, max_value=30.0)
    r = admin_client_logged.post(
        reverse("admin:monitoring_metricdefinition_change", args=[d.pk]),
        {"label_override": "12 V", "min_override": "5", "max_override": ""},
    )
    assert r.status_code == 302
    d.refresh_from_db()
    assert d.min_override == 5.0
    assert d.label_override == "12 V"
    assert (d.min_value, d.max_value) == (0.0, 30.0)
