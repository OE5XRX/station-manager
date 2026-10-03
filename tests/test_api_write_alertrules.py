"""Permission-matrix + audit tests for AlertRule write endpoints.

Task 8: region-mgr/staff full CRUD.  Every mutation produces a CONFIG_CHANGED
AccountAuditLog row with "via API token" in the message.

AlertRule is a global config resource — gated purely by role (is_any_region_manager),
no topology/station scope.

Design notes:
- alert_type is UNIQUE; the seed migration (0002_seed_default_rules.py) creates 6 rows:
  station_offline, cpu_temperature, disk_warning, disk_critical, ram_critical, ota_failed.
- Unused AlertTypes (not seeded): unexpected_reboot, power_warning, storage_health.
  CREATE tests use unexpected_reboot. To avoid unique-constraint collisions within the
  test session we delete any existing row of that type before POSTing in each create test.
- UPDATE tests PATCH an existing seeded rule (cpu_temp_alert_rule fixture).
- DELETE tests create a fresh unexpected_reboot row and then DELETE it.
- Numeric threshold posted as JSON number — JSON is always dot-decimal, so the
  de-locale decimal-comma issue does not apply to JSON request bodies.
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CREATE_TYPE = "unexpected_reboot"
_CREATE_PAYLOAD = {
    "alert_type": _CREATE_TYPE,
    "threshold": 0.0,
    "severity": "critical",
    "is_active": True,
    "description": "Unexpected reboot detection",
}


def _ensure_no_rule(alert_type):
    """Delete any existing rule of this alert_type (seed or leftover) so POST won't 409."""
    from apps.monitoring.models import AlertRule

    AlertRule.objects.filter(alert_type=alert_type).delete()


def _create_rule(alert_type=_CREATE_TYPE):
    """Create a fresh AlertRule row for update/delete tests."""
    from apps.monitoring.models import AlertRule

    _ensure_no_rule(alert_type)
    return AlertRule.objects.create(
        alert_type=alert_type,
        threshold=0.0,
        severity=AlertRule.Severity.CRITICAL,
        is_active=True,
        description="Temp rule for test",
    )


# ---------------------------------------------------------------------------
# PATCH (update) permission matrix — use the seeded cpu_temp_alert_rule
# ---------------------------------------------------------------------------


def test_update_region_mgr_allowed(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["region_mgr"]).patch(url, {"threshold": 85.0}, format="json")
    assert resp.status_code == 200
    cpu_temp_alert_rule.refresh_from_db()
    assert cpu_temp_alert_rule.threshold == 85.0


def test_update_staff_allowed(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["staff"]).patch(url, {"threshold": 90.0}, format="json")
    assert resp.status_code == 200


def test_update_admin_allowed(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["admin"]).patch(url, {"threshold": 75.0}, format="json")
    assert resp.status_code == 200


def test_update_station_user_forbidden(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["station_user"]).patch(url, {"threshold": 70.0}, format="json")
    assert resp.status_code == 403


def test_update_applicant_forbidden(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["applicant"]).patch(url, {"threshold": 70.0}, format="json")
    assert resp.status_code == 403


def test_update_anon_unauthorized(api_topology, anon_client, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = anon_client.patch(url, {"threshold": 70.0}, format="json")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST (create) permission matrix — uses unexpected_reboot (not seeded)
# ---------------------------------------------------------------------------


def test_create_staff_allowed(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["staff"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 201
    assert resp.data["alert_type"] == _CREATE_TYPE


def test_create_region_mgr_allowed(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["region_mgr"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 201


def test_create_admin_allowed(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["admin"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 201


def test_create_station_user_forbidden(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["station_user"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 403


def test_create_applicant_forbidden(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["applicant"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 403


def test_create_anon_unauthorized(api_topology, anon_client):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = anon_client.post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE permission matrix
# ---------------------------------------------------------------------------


def test_delete_staff_allowed(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["staff"]).delete(url)
    assert resp.status_code == 204


def test_delete_region_mgr_allowed(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["region_mgr"]).delete(url)
    assert resp.status_code == 204


def test_delete_station_user_forbidden(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["station_user"]).delete(url)
    assert resp.status_code == 403


def test_delete_applicant_forbidden(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["applicant"]).delete(url)
    assert resp.status_code == 403


def test_delete_anon_unauthorized(api_topology, anon_client):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = anon_client.delete(url)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Audit: write → CONFIG_CHANGED AccountAuditLog row with "via API token"
# ---------------------------------------------------------------------------


def test_update_produces_audit_row(api_topology, bearer, cpu_temp_alert_rule):
    from apps.accounts.models import AccountAuditLog

    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    bearer(api_topology["region_mgr"]).patch(url, {"threshold": 82.0}, format="json")
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "AlertRule" in log.message
    assert "cpu_temperature" in log.message


def test_create_produces_audit_row(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog

    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    bearer(api_topology["staff"]).post(url, _CREATE_PAYLOAD, format="json")
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "AlertRule" in log.message
    assert _CREATE_TYPE in log.message


def test_delete_produces_audit_row(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog

    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    bearer(api_topology["region_mgr"]).delete(url)
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "AlertRule" in log.message


# ---------------------------------------------------------------------------
# Threshold posted as JSON number (not comma-decimal string)
# ---------------------------------------------------------------------------


def test_threshold_as_json_number(api_topology, bearer, cpu_temp_alert_rule):
    """Posting threshold as a JSON number (82.5) is accepted and stored correctly.

    JSON bodies are always dot-decimal — no de-locale conversion needed.
    """
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["region_mgr"]).patch(url, {"threshold": 82.5}, format="json")
    assert resp.status_code == 200
    assert resp.data["threshold"] == 82.5
