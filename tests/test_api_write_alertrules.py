"""Permission-matrix + audit tests for AlertRule write endpoints.

alert_rules.manage (Admin) full CRUD; staff/region-mgr get 403.  Every
mutation produces a CONFIG_CHANGED AccountAuditLog row with "via API token" in the message.

AlertRule is a global config resource — gated purely by alert_rules.manage,
no topology/station scope.

Design notes:
- Event alert_types are unique (conditional UniqueConstraint; any number of
  ``threshold`` rules is allowed). The migrations seed the event rules and convert
  the legacy CPU/Disk/RAM rules to threshold rules (monitoring.0006).
  CREATE tests use unexpected_reboot. To avoid unique-constraint collisions within the
  test session we delete any existing row of that type before POSTing in each create test.
- UPDATE tests PATCH the seeded ``cpu.temp_c`` threshold rule (cpu_temp_alert_rule fixture).
- DELETE tests create a fresh unexpected_reboot row and then DELETE it.
- Numeric threshold posted as JSON number — JSON is always dot-decimal, so the
  de-locale decimal-comma issue does not apply to JSON request bodies.
"""

import pytest
from django.urls import reverse

from tests.test_api_read_fixtures import session

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


def test_update_region_mgr_forbidden(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["region_mgr"]).patch(url, {"threshold": 85.0}, format="json")
    assert resp.status_code == 403
    assert resp.json()["required"] == "alert_rules.manage"
    cpu_temp_alert_rule.refresh_from_db()
    assert cpu_temp_alert_rule.threshold != 85.0


def test_update_staff_forbidden(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["staff"]).patch(url, {"threshold": 90.0}, format="json")
    assert resp.status_code == 403


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
    resp = session(api_topology["applicant"]).patch(url, {"threshold": 70.0}, format="json")
    assert resp.status_code == 403


def test_update_anon_unauthorized(api_topology, anon_client, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = anon_client.patch(url, {"threshold": 70.0}, format="json")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST (create) permission matrix — uses unexpected_reboot (not seeded)
# ---------------------------------------------------------------------------


def test_create_staff_forbidden(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["staff"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 403
    assert resp.json()["required"] == "alert_rules.manage"


def test_create_region_mgr_forbidden(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["region_mgr"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 403


def test_create_admin_allowed(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["admin"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 201


def test_create_duplicate_event_type_rejected(api_topology, bearer):
    """Event types stay unique via the conditional UniqueConstraint -> 400, never a 500."""
    _create_rule()
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["admin"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 400


def test_create_station_user_forbidden(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["station_user"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 403


def test_create_applicant_forbidden(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = session(api_topology["applicant"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 403


def test_create_anon_unauthorized(api_topology, anon_client):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = anon_client.post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE permission matrix
# ---------------------------------------------------------------------------


def test_delete_staff_forbidden(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["staff"]).delete(url)
    assert resp.status_code == 403


def test_delete_region_mgr_forbidden(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["region_mgr"]).delete(url)
    assert resp.status_code == 403


def test_delete_station_user_forbidden(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["station_user"]).delete(url)
    assert resp.status_code == 403


def test_delete_applicant_forbidden(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = session(api_topology["applicant"]).delete(url)
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
    bearer(api_topology["admin"]).patch(url, {"threshold": 82.0}, format="json")
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "AlertRule" in log.message
    assert "threshold" in log.message


def test_create_produces_audit_row(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog

    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    bearer(api_topology["admin"]).post(url, _CREATE_PAYLOAD, format="json")
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
    bearer(api_topology["admin"]).delete(url)
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
    resp = bearer(api_topology["admin"]).patch(url, {"threshold": 82.5}, format="json")
    assert resp.status_code == 200
    assert resp.data["threshold"] == 82.5


# ---------------------------------------------------------------------------
# Threshold rule fields + validation (spec §5.1 / §7)
# ---------------------------------------------------------------------------


@pytest.fixture
def disk_defs(db):
    from apps.monitoring.models import MetricDefinition

    for key in ("disk.root.used_pct", "disk.data.used_pct"):
        MetricDefinition.objects.update_or_create(
            key=key, defaults={"label": key, "unit": "%", "group": "disk"}
        )


_THRESHOLD_PAYLOAD = {
    "alert_type": "threshold",
    "name": "Disk full",
    "metric_key": "disk.*.used_pct",
    "operator": "gte",
    "aggregate": "max",
    "duration_seconds": 120,
    "threshold": 90.0,
    "severity": "critical",
    "is_active": True,
}


def test_read_exposes_threshold_fields(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["staff"]).get(url)
    assert resp.status_code == 200
    for field in ("name", "metric_key", "operator", "aggregate", "duration_seconds"):
        assert field in resp.data
    assert resp.data["metric_key"] == "cpu.temp_c"


def test_create_threshold_rule_with_pattern(api_topology, bearer, disk_defs):
    resp = bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), _THRESHOLD_PAYLOAD, format="json"
    )
    assert resp.status_code == 201, resp.content
    assert resp.data["metric_key"] == "disk.*.used_pct"
    assert resp.data["aggregate"] == "max"
    assert resp.data["duration_seconds"] == 120


def test_create_second_threshold_rule_allowed(api_topology, bearer, disk_defs):
    client = bearer(api_topology["admin"])
    url = reverse("api:alert-rule-list")
    assert client.post(url, _THRESHOLD_PAYLOAD, format="json").status_code == 201
    second = {**_THRESHOLD_PAYLOAD, "threshold": 80.0, "severity": "warning"}
    assert client.post(url, second, format="json").status_code == 201


def test_create_threshold_rule_audit_uses_str(api_topology, bearer, disk_defs):
    from apps.accounts.models import AccountAuditLog

    bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), _THRESHOLD_PAYLOAD, format="json"
    )
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "Disk full" in log.message


def test_create_threshold_rule_unknown_metric_key(api_topology, bearer, disk_defs):
    payload = {**_THRESHOLD_PAYLOAD, "metric_key": "nope.does_not_exist"}
    resp = bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), payload, format="json"
    )
    assert resp.status_code == 400
    assert "metric_key" in resp.data


def test_create_threshold_rule_invalid_pattern(api_topology, bearer, disk_defs):
    payload = {**_THRESHOLD_PAYLOAD, "metric_key": "disk.*.*"}
    resp = bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), payload, format="json"
    )
    assert resp.status_code == 400
    assert "metric_key" in resp.data


def test_create_threshold_rule_requires_metric_key(api_topology, bearer, disk_defs):
    payload = {k: v for k, v in _THRESHOLD_PAYLOAD.items() if k != "metric_key"}
    resp = bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), payload, format="json"
    )
    assert resp.status_code == 400
    assert "metric_key" in resp.data


def test_create_threshold_rule_requires_operator(api_topology, bearer, disk_defs):
    payload = {k: v for k, v in _THRESHOLD_PAYLOAD.items() if k != "operator"}
    resp = bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), payload, format="json"
    )
    assert resp.status_code == 400
    assert "operator" in resp.data


def test_create_event_rule_with_metric_key_rejected(api_topology, bearer, disk_defs):
    _ensure_no_rule(_CREATE_TYPE)
    payload = {**_CREATE_PAYLOAD, "metric_key": "disk.root.used_pct"}
    resp = bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), payload, format="json"
    )
    assert resp.status_code == 400
    assert "metric_key" in resp.data


def test_create_event_rule_with_operator_rejected(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    payload = {**_CREATE_PAYLOAD, "operator": "gt"}
    resp = bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), payload, format="json"
    )
    assert resp.status_code == 400
    assert "operator" in resp.data


def test_create_duplicate_ota_failed_rejected(api_topology, bearer, ota_failed_alert_rule):
    payload = {"alert_type": "ota_failed", "threshold": 0, "severity": "warning"}
    resp = bearer(api_topology["admin"]).post(
        reverse("api:alert-rule-list"), payload, format="json"
    )
    assert resp.status_code == 400
    assert "alert_type" in resp.data or "non_field_errors" in resp.data


def test_patch_threshold_rule_metric_key_validated(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["admin"]).patch(url, {"metric_key": "bad key"}, format="json")
    assert resp.status_code == 400
    assert "metric_key" in resp.data


def test_patch_threshold_rule_to_event_type_with_metric_key_rejected(
    api_topology, bearer, cpu_temp_alert_rule
):
    """Switching a threshold rule to an event type keeps its metric_key -> 400."""
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["admin"]).patch(url, {"alert_type": _CREATE_TYPE}, format="json")
    assert resp.status_code == 400


def test_patch_threshold_rule_duration(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    resp = bearer(api_topology["admin"]).patch(url, {"duration_seconds": 300}, format="json")
    assert resp.status_code == 200, resp.content
    cpu_temp_alert_rule.refresh_from_db()
    assert cpu_temp_alert_rule.duration_seconds == 300


def test_patch_rule_with_undescribed_key_threshold_only(api_topology, bearer):
    """Seeded 12 V rule references a key without a definition yet; editing
    only the threshold must still work (key unchanged -> not re-validated)."""
    from apps.monitoring.models import AlertRule, MetricDefinition

    MetricDefinition.objects.filter(key="pwr.12v.voltage_v").delete()
    rule = AlertRule.objects.create(
        alert_type="threshold",
        metric_key="pwr.12v.voltage_v",
        operator="lt",
        threshold=11.0,
        severity="warning",
    )
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["admin"]).patch(url, {"threshold": 10.5}, format="json")
    assert resp.status_code == 200, resp.content


def test_patch_rolls_back_with_failed_reset(
    api_topology, bearer, cpu_temp_alert_rule, monkeypatch
):
    """Review round 3: the UPDATE and the post_save reset commit together."""
    from apps.monitoring import signals

    def boom(*args, **kwargs):
        raise RuntimeError("reset failed")

    monkeypatch.setattr(signals, "reset_rule_evaluation", boom)
    before = cpu_temp_alert_rule.threshold
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    with pytest.raises(RuntimeError):
        bearer(api_topology["admin"]).patch(url, {"threshold": before + 1}, format="json")
    cpu_temp_alert_rule.refresh_from_db()
    assert cpu_temp_alert_rule.threshold == before


# ---------------------------------------------------------------------------
# Audit parity with the UI: changes diff on update, config snapshot on create/delete
# ---------------------------------------------------------------------------


def _latest_api_audit():
    from apps.accounts.models import AccountAuditLog

    return AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")


def test_update_audit_records_changes(api_topology, bearer, cpu_temp_alert_rule):
    url = reverse("api:alert-rule-detail", args=[cpu_temp_alert_rule.pk])
    old = cpu_temp_alert_rule.threshold
    resp = bearer(api_topology["admin"]).patch(
        url, {"threshold": old + 3.0, "name": cpu_temp_alert_rule.name}, format="json"
    )
    assert resp.status_code == 200
    log = _latest_api_audit()
    assert log.details["action"] == "updated"
    assert log.details["changes"] == {"threshold": {"old": old, "new": old + 3.0}}


def test_create_audit_records_config(api_topology, bearer):
    _ensure_no_rule(_CREATE_TYPE)
    url = reverse("api:alert-rule-list")
    resp = bearer(api_topology["admin"]).post(url, _CREATE_PAYLOAD, format="json")
    assert resp.status_code == 201
    log = _latest_api_audit()
    assert log.details["action"] == "created"
    assert log.details["config"]["alert_type"] == _CREATE_TYPE
    assert log.details["config"]["severity"] == "critical"


def test_delete_audit_records_config(api_topology, bearer):
    rule = _create_rule()
    url = reverse("api:alert-rule-detail", args=[rule.pk])
    resp = bearer(api_topology["admin"]).delete(url)
    assert resp.status_code == 204
    log = _latest_api_audit()
    assert log.details["action"] == "deleted"
    assert log.details["config"]["alert_type"] == _CREATE_TYPE
    assert log.details["config"]["description"] == "Temp rule for test"
