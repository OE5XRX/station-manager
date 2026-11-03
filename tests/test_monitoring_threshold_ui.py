"""UI: threshold rule create/update/delete on the alert settings page (Task 6)."""

import pytest
from django.urls import reverse
from django.utils import translation

from apps.accounts.models import AccountAuditLog
from apps.monitoring.models import Alert, AlertRule, MetricDefinition, ThresholdState

THRESHOLD = AlertRule.AlertType.THRESHOLD


def _payload(**overrides):
    data = {
        "name": "Hot CPU",
        "metric_key": "cpu.temp_c",
        "operator": "gte",
        "aggregate": "avg",
        "threshold": "72.5",
        "duration_seconds": "120",
        "severity": "critical",
        "is_active": "on",
        "description": "",
    }
    data.update(overrides)
    return data


@pytest.fixture
def defs(db):
    for key, unit in (
        ("cpu.temp_c", "°C"),
        ("disk.root.used_pct", "%"),
        ("disk.data.used_pct", "%"),
        ("pwr.12v.voltage_v", "V"),
    ):
        MetricDefinition.objects.update_or_create(key=key, defaults={"unit": unit, "label": key})


@pytest.fixture
def rule(defs):
    return AlertRule.objects.create(
        alert_type=THRESHOLD,
        name="Undervoltage test",
        metric_key="pwr.12v.voltage_v",
        operator="lt",
        aggregate="avg",
        threshold=11.0,
        duration_seconds=60,
        severity="warning",
    )


@pytest.mark.django_db
class TestCreate:
    def test_manager_creates_rule(self, client, admin_user, defs):
        client.force_login(admin_user)
        before = AlertRule.objects.filter(alert_type=THRESHOLD).count()
        resp = client.post(reverse("monitoring:threshold_rule_create"), _payload())
        assert resp.status_code == 302
        assert AlertRule.objects.filter(alert_type=THRESHOLD).count() == before + 1
        created = AlertRule.objects.get(name="Hot CPU")
        assert created.alert_type == THRESHOLD
        assert created.threshold == 72.5
        assert created.duration_seconds == 120
        assert created.severity == "critical"
        assert created.is_active is True
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.CONFIG_CHANGED, details__object="alert_rule"
        ).exists()

    def test_pattern_key_accepted(self, client, admin_user, defs):
        client.force_login(admin_user)
        resp = client.post(
            reverse("monitoring:threshold_rule_create"),
            _payload(name="Disk pattern", metric_key="disk.*.used_pct"),
        )
        assert resp.status_code == 302
        assert AlertRule.objects.filter(name="Disk pattern").exists()

    def test_alert_type_cannot_be_smuggled(self, client, admin_user, defs):
        client.force_login(admin_user)
        client.post(
            reverse("monitoring:threshold_rule_create"),
            _payload(name="Smuggle", alert_type="station_offline"),
        )
        assert AlertRule.objects.get(name="Smuggle").alert_type == THRESHOLD

    def test_invalid_metric_key_shows_error(self, client, admin_user, defs):
        client.force_login(admin_user)
        before = AlertRule.objects.count()
        resp = client.post(
            reverse("monitoring:threshold_rule_create"),
            _payload(name="Bogus", metric_key="nope.unknown_key"),
        )
        assert resp.status_code == 400
        assert AlertRule.objects.count() == before
        content = resp.content.decode()
        assert "No known metric matches" in content
        # Bound value survives the re-render.
        assert 'value="nope.unknown_key"' in content

    def test_viewer_cannot_create(self, client, staff_user, defs):
        client.force_login(staff_user)
        before = AlertRule.objects.count()
        resp = client.post(reverse("monitoring:threshold_rule_create"), _payload())
        assert resp.status_code == 403
        assert AlertRule.objects.count() == before

    def test_get_not_allowed(self, client, admin_user):
        client.force_login(admin_user)
        resp = client.get(reverse("monitoring:threshold_rule_create"))
        assert resp.status_code == 405


@pytest.mark.django_db
class TestUpdate:
    def test_update_resets_open_alert(self, client, admin_user, rule, station):
        alert = Alert.objects.create(
            station=station, alert_rule=rule, severity="warning", title="t", message="m"
        )
        ThresholdState.objects.create(
            rule=rule, station=station, metric_key="pwr.12v.voltage_v", alert=alert
        )
        client.force_login(admin_user)
        resp = client.post(
            reverse("monitoring:threshold_rule_update", args=[rule.pk]),
            _payload(name=rule.name, metric_key=rule.metric_key, operator="lt", threshold="10.5"),
        )
        assert resp.status_code == 302
        rule.refresh_from_db()
        alert.refresh_from_db()
        assert rule.threshold == 10.5
        assert alert.is_resolved is True
        assert not ThresholdState.objects.filter(rule=rule).exists()

    def test_update_invalid_rerenders_400(self, client, admin_user, rule):
        client.force_login(admin_user)
        resp = client.post(
            reverse("monitoring:threshold_rule_update", args=[rule.pk]),
            _payload(metric_key="BAD KEY"),
        )
        assert resp.status_code == 400
        rule.refresh_from_db()
        assert rule.metric_key == "pwr.12v.voltage_v"
        assert "Enter a metric key" in resp.content.decode()

    def test_viewer_cannot_update(self, client, staff_user, rule):
        client.force_login(staff_user)
        resp = client.post(
            reverse("monitoring:threshold_rule_update", args=[rule.pk]),
            _payload(threshold="1"),
        )
        assert resp.status_code == 403
        rule.refresh_from_db()
        assert rule.threshold == 11.0

    def test_event_rule_pk_404(self, client, admin_user, offline_alert_rule):
        client.force_login(admin_user)
        resp = client.post(
            reverse("monitoring:threshold_rule_update", args=[offline_alert_rule.pk]),
            _payload(),
        )
        assert resp.status_code == 404
        resp = client.post(
            reverse("monitoring:threshold_rule_delete", args=[offline_alert_rule.pk])
        )
        assert resp.status_code == 404
        assert AlertRule.objects.filter(pk=offline_alert_rule.pk).exists()

    def test_legacy_update_view_404s_for_threshold_rule(self, client, admin_user, rule):
        client.force_login(admin_user)
        resp = client.post(
            reverse("monitoring:alert_rule_update", args=[rule.pk]), {"threshold": "5"}
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestDelete:
    def test_delete_resolves_and_removes(self, client, admin_user, rule, station):
        alert = Alert.objects.create(
            station=station, alert_rule=rule, severity="warning", title="t", message="m"
        )
        client.force_login(admin_user)
        resp = client.post(reverse("monitoring:threshold_rule_delete", args=[rule.pk]))
        assert resp.status_code == 302
        assert not AlertRule.objects.filter(pk=rule.pk).exists()
        alert.refresh_from_db()
        assert alert.is_resolved is True

    def test_viewer_cannot_delete(self, client, staff_user, rule):
        client.force_login(staff_user)
        resp = client.post(reverse("monitoring:threshold_rule_delete", args=[rule.pk]))
        assert resp.status_code == 403
        assert AlertRule.objects.filter(pk=rule.pk).exists()


@pytest.mark.django_db
class TestSettingsPage:
    def test_sections_and_number_inputs(self, client, admin_user, rule):
        client.force_login(admin_user)
        with translation.override("de"):
            resp = client.get(reverse("monitoring:alert_settings"))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "{#" not in content and "#}" not in content
        assert "Undervoltage test" in content
        # Every number input is dot-decimal + lang="en".
        import re

        inputs = re.findall(r"<input[^>]*type=\"number\"[^>]*>", content)
        assert inputs, "no number inputs rendered"
        for tag in inputs:
            assert 'lang="en"' in tag, tag
            # Integer seconds use step="1"; every other number input is step="any".
            expected_step = 'step="1"' if 'name="duration_seconds"' in tag else 'step="any"'
            assert expected_step in tag, tag
        assert 'value="11.0"' in content
        assert 'value="11,0"' not in content
        # Empty create form never leaks Python's None into inputs.
        assert 'value="None"' not in content
        assert ">None<" not in content
        # Datalist with concrete keys + pattern suggestion.
        assert "<datalist" in content
        assert 'value="disk.*.used_pct"' in content
        assert 'value="cpu.temp_c"' in content

    def test_event_rules_exclude_threshold(self, client, admin_user, rule):
        client.force_login(admin_user)
        resp = client.get(reverse("monitoring:alert_settings"))
        event_rules = list(resp.context["event_rules"])
        threshold_rules = list(resp.context["threshold_rules"])
        assert all(r.alert_type != THRESHOLD for r in event_rules)
        assert rule in threshold_rules

    def test_viewer_sees_readonly_rules(self, client, staff_user, rule):
        client.force_login(staff_user)
        resp = client.get(reverse("monitoring:alert_settings"))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Undervoltage test" in content
        assert reverse("monitoring:threshold_rule_create") not in content
        assert reverse("monitoring:threshold_rule_delete", args=[rule.pk]) not in content
