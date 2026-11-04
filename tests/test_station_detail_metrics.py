"""Station detail: generic Metrics tab (Task 6)."""

import pytest
from django.urls import reverse

from apps.monitoring.models import Alert, AlertRule, MetricDefinition, ThresholdState
from apps.stations.models import Station, StationTelemetry


def _defs():
    rows = [
        ("pwr.12v.voltage_v", "12 V rail voltage", "V", "power", 2),
        ("pwr.12v.current_a", "12 V rail current", "A", "power", 3),
        ("cpu.temp_c", "CPU temperature", "°C", "system", 1),
        ("net.eth0.rx_kbps", "eth0 receive", "kbit/s", "network", 0),
        ("secret.hidden_v", "Hidden metric", "V", "other", 1),
        ("bad.blocked_v", "Blocked metric", "V", "other", 1),
    ]
    for key, label, unit, group, precision in rows:
        MetricDefinition.objects.update_or_create(
            key=key,
            defaults={
                "label": label,
                "unit": unit,
                "group": group,
                "precision": precision,
                "is_hidden": key.startswith("secret."),
                "is_blocked": key.startswith("bad."),
            },
        )


def _telemetry(station):
    StationTelemetry.objects.create(
        station=station,
        metrics={
            "pwr.12v.voltage_v": {"min": 10.5, "max": 12.25, "avg": 10.875, "ts": "t"},
            "pwr.12v.current_a": {"min": 0.4, "max": 0.4, "avg": 0.4, "ts": "t"},
            "cpu.temp_c": {"min": 51.0, "max": 51.0, "avg": 51.0, "ts": "t"},
            "net.eth0.rx_kbps": {"min": 3.0, "max": 3.0, "avg": 3.0, "ts": "t"},
            "secret.hidden_v": {"min": 1.0, "max": 1.0, "avg": 1.0, "ts": "t"},
            "bad.blocked_v": {"min": 1.0, "max": 1.0, "avg": 1.0, "ts": "t"},
            "undefined.key_x": {"min": 7.0, "max": 7.0, "avg": 7.0, "ts": "t"},
        },
    )


@pytest.mark.django_db
def test_metrics_tab_renders_groups_and_values(client, admin_user):
    _defs()
    station = Station.objects.create(name="OE5M")
    _telemetry(station)
    client.force_login(admin_user)
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert 'data-tab="metrics"' in content
    assert 'data-tab-panel="metrics"' in content
    # Power group with min/avg/max at the definition's precision.
    assert "12 V rail voltage" in content
    assert "10.50" in content and "10.88" in content and "12.25" in content
    # Scalar shows a single value.
    assert "0.400" in content
    groups = resp.context["metric_groups"]
    labels = [g[0] for g in groups]
    # Ordered system, network, power, ...
    assert labels.index("System") < labels.index("Network") < labels.index("Power")
    power = dict(groups)["Power"]
    volt = next(r for r in power if r["key"] == "pwr.12v.voltage_v")
    amp = next(r for r in power if r["key"] == "pwr.12v.current_a")
    assert volt["is_aggregate"] is True
    assert amp["is_aggregate"] is False
    # Hidden, blocked and undefined keys never shown.
    all_keys = {r["key"] for _, rows in groups for r in rows}
    assert "secret.hidden_v" not in all_keys
    assert "bad.blocked_v" not in all_keys
    assert "undefined.key_x" not in all_keys
    assert "Hidden metric" not in content


@pytest.mark.django_db
def test_breached_metric_highlighted(client, admin_user):
    _defs()
    station = Station.objects.create(name="OE5N")
    _telemetry(station)
    rule = AlertRule.objects.create(
        alert_type=AlertRule.AlertType.THRESHOLD,
        name="UV",
        metric_key="pwr.12v.voltage_v",
        operator="lt",
        threshold=11.0,
        severity="critical",
    )
    alert = Alert.objects.create(
        station=station, alert_rule=rule, severity="critical", title="t", message="m"
    )
    ThresholdState.objects.create(
        rule=rule, station=station, metric_key="pwr.12v.voltage_v", alert=alert
    )
    # A resolved alert on another key must not highlight it.
    other = Alert.objects.create(
        station=station,
        alert_rule=rule,
        severity="warning",
        title="t",
        message="m",
        is_resolved=True,
    )
    ThresholdState.objects.create(rule=rule, station=station, metric_key="cpu.temp_c", alert=other)
    client.force_login(admin_user)
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    rows = {r["key"]: r for _, rs in resp.context["metric_groups"] for r in rs}
    assert rows["pwr.12v.voltage_v"]["breached"] == "critical"
    assert not rows["cpu.temp_c"]["breached"]
    content = resp.content.decode()
    assert "is-breached" in content
    assert "1 metric breached" in content
    assert "rule breached" not in content


@pytest.mark.django_db
def test_metrics_empty_state(client, admin_user):
    station = Station.objects.create(name="OE5E")
    client.force_login(admin_user)
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert resp.status_code == 200
    assert resp.context["metric_groups"] == []
    assert "No metrics reported yet" in resp.content.decode()


@pytest.mark.django_db
def test_inventory_links_to_metrics_tab(client, admin_user):
    station = Station.objects.create(name="OE5L")
    client.force_login(admin_user)
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert 'data-tab-link="metrics"' in resp.content.decode()
