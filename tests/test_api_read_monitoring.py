"""Tests for AlertRule + Alert read endpoints (Task 7)."""

import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401


@pytest.mark.django_db
def test_alert_rule_list_member_and_new_types(topology):  # noqa: F811
    from apps.monitoring.models import AlertRule

    AlertRule.objects.get_or_create(
        alert_type="unexpected_reboot", defaults={"threshold": 1, "severity": "warning"}
    )
    resp = bearer(topology["station_user"]).get("/api/v1/alert-rules/")
    assert resp.status_code == 200
    assert "unexpected_reboot" in {r["alert_type"] for r in resp.data["results"]}


@pytest.mark.django_db
def test_alert_scope(topology):  # noqa: F811
    from apps.monitoring.models import Alert

    Alert.objects.create(
        station=topology["station_out"], severity="warning", title="x", message="y"
    )
    a_in = Alert.objects.create(
        station=topology["station_in"], severity="critical", title="z", message="w"
    )
    resp = bearer(topology["station_user"]).get("/api/v1/alerts/")
    assert resp.status_code == 200
    ids = {a["id"] for a in resp.data["results"]}
    assert ids == {a_in.id}
