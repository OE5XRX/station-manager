"""API tests for generic metrics (spec §7): metric definitions endpoint,
station telemetry ``metrics`` and the schema."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def metric_defs(db):
    from apps.monitoring.models import MetricDefinition

    visible, _ = MetricDefinition.objects.update_or_create(
        key="cpu.temp_c",
        defaults={
            "label": "CPU temperature",
            "unit": "°C",
            "group": "system",
            "precision": 1,
            "min_value": -40.0,
            "max_value": 150.0,
            "is_hidden": False,
            "is_blocked": False,
        },
    )
    hidden, _ = MetricDefinition.objects.update_or_create(
        key="sys.load1",
        defaults={"label": "Load 1", "unit": "", "group": "system", "is_hidden": True},
    )
    blocked, _ = MetricDefinition.objects.update_or_create(
        key="pwr.12v.voltage_v",
        defaults={"label": "12V", "unit": "V", "group": "power", "is_blocked": True},
    )
    return {"visible": visible, "hidden": hidden, "blocked": blocked}


def _list_url():
    return reverse("api:metric-definition-list")


def _detail_url(pk):
    return reverse("api:metric-definition-detail", args=[pk])


# ---------------------------------------------------------------------------
# Definitions: read / write permissions
# ---------------------------------------------------------------------------


def test_viewer_can_list_definitions(api_topology, bearer, metric_defs):
    resp = bearer(api_topology["staff"]).get(_list_url())
    assert resp.status_code == 200
    rows = {r["key"]: r for r in resp.data["results"]}
    row = rows["cpu.temp_c"]
    assert row["display_label"] == "CPU temperature"
    assert row["range_min"] == -40.0
    assert row["range_max"] == 150.0
    assert row["unit"] == "°C"


def test_viewer_can_retrieve_definition(api_topology, bearer, metric_defs):
    resp = bearer(api_topology["staff"]).get(_detail_url(metric_defs["visible"].pk))
    assert resp.status_code == 200
    assert resp.data["key"] == "cpu.temp_c"


def test_viewer_cannot_patch_definition(api_topology, bearer, metric_defs):
    resp = bearer(api_topology["staff"]).patch(
        _detail_url(metric_defs["visible"].pk), {"label_override": "X"}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["required"] == "alert_rules.manage"


def test_anon_cannot_list_definitions(anon_client, metric_defs):
    assert anon_client.get(_list_url()).status_code == 401


def test_manager_patch_override_audited(api_topology, bearer, metric_defs):
    from apps.accounts.models import AccountAuditLog

    d = metric_defs["visible"]
    resp = bearer(api_topology["admin"]).patch(
        _detail_url(d.pk),
        {"label_override": "CPU-Temperatur", "max_override": 120.0, "is_hidden": True},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.data["display_label"] == "CPU-Temperatur"
    assert resp.data["range_max"] == 120.0
    d.refresh_from_db()
    assert d.label_override == "CPU-Temperatur"
    assert d.max_override == 120.0
    assert d.is_hidden is True
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "cpu.temp_c" in log.message


def test_agent_fields_read_only(api_topology, bearer, metric_defs):
    d = metric_defs["visible"]
    resp = bearer(api_topology["admin"]).patch(
        _detail_url(d.pk),
        {"key": "evil.key", "label": "hacked", "unit": "K", "min_value": 0},
        format="json",
    )
    assert resp.status_code == 200
    d.refresh_from_db()
    assert d.key == "cpu.temp_c"
    assert d.label == "CPU temperature"
    assert d.unit == "°C"
    assert d.min_value == -40.0


def test_min_override_above_max_override_rejected(api_topology, bearer, metric_defs):
    resp = bearer(api_topology["admin"]).patch(
        _detail_url(metric_defs["visible"].pk),
        {"min_override": 100.0, "max_override": 10.0},
        format="json",
    )
    assert resp.status_code == 400
    assert "min_override" in resp.data


def test_min_override_checked_against_existing_max(api_topology, bearer, metric_defs):
    d = metric_defs["visible"]
    d.max_override = 50.0
    d.save()
    resp = bearer(api_topology["admin"]).patch(
        _detail_url(d.pk), {"min_override": 60.0}, format="json"
    )
    assert resp.status_code == 400


def test_post_definition_not_allowed(api_topology, bearer, metric_defs):
    resp = bearer(api_topology["admin"]).post(
        _list_url(), {"key": "foo.bar", "label": "x"}, format="json"
    )
    assert resp.status_code == 405


def test_delete_definition_not_allowed(api_topology, bearer, metric_defs):
    resp = bearer(api_topology["admin"]).delete(_detail_url(metric_defs["visible"].pk))
    assert resp.status_code == 405


def test_filter_definitions_by_hidden(api_topology, bearer, metric_defs):
    resp = bearer(api_topology["staff"]).get(_list_url(), {"is_hidden": "true"})
    assert resp.status_code == 200
    keys = {r["key"] for r in resp.data["results"]}
    assert "sys.load1" in keys
    assert "cpu.temp_c" not in keys


def test_filter_definitions_by_group(api_topology, bearer, metric_defs):
    resp = bearer(api_topology["staff"]).get(_list_url(), {"group": "power"})
    keys = {r["key"] for r in resp.data["results"]}
    assert keys == {"pwr.12v.voltage_v"}


# ---------------------------------------------------------------------------
# Station telemetry: metrics
# ---------------------------------------------------------------------------


def test_telemetry_shows_visible_metrics_only(api_topology, bearer, metric_defs):
    from apps.stations.models import StationTelemetry

    station = api_topology["station_in"]
    StationTelemetry.objects.create(
        station=station,
        metrics={
            "cpu.temp_c": {"min": 50.0, "max": 52.0, "avg": 51.0, "ts": "2026-11-03T10:00:00Z"},
            "sys.load1": {"min": 0.1, "max": 0.1, "avg": 0.1, "ts": "2026-11-03T10:00:00Z"},
            "pwr.12v.voltage_v": {"min": 12, "max": 12, "avg": 12, "ts": "2026-11-03T10:00:00Z"},
            "unknown.key": {"min": 1, "max": 1, "avg": 1, "ts": "2026-11-03T10:00:00Z"},
        },
    )
    resp = bearer(api_topology["admin"]).get(f"/api/v1/stations/{station.pk}/telemetry/")
    assert resp.status_code == 200
    metrics = resp.data["metrics"]
    assert [m["key"] for m in metrics] == ["cpu.temp_c"]
    m = metrics[0]
    assert m["label"] == "CPU temperature"
    assert m["unit"] == "°C"
    assert m["group"] == "system"
    assert m["precision"] == 1
    assert (m["min"], m["max"], m["avg"]) == (50.0, 52.0, 51.0)
    assert m["ts"] == "2026-11-03T10:00:00Z"


def test_telemetry_metrics_use_label_override(api_topology, bearer, metric_defs):
    from apps.stations.models import StationTelemetry

    d = metric_defs["visible"]
    d.label_override = "CPU-Temperatur"
    d.save()
    station = api_topology["station_in"]
    StationTelemetry.objects.create(
        station=station,
        metrics={"cpu.temp_c": {"min": 1, "max": 1, "avg": 1, "ts": "2026-11-03T10:00:00Z"}},
    )
    resp = bearer(api_topology["admin"]).get(f"/api/v1/stations/{station.pk}/telemetry/")
    assert resp.data["metrics"][0]["label"] == "CPU-Temperatur"


def test_telemetry_metrics_empty(api_topology, bearer):
    from apps.stations.models import StationTelemetry

    station = api_topology["station_in"]
    StationTelemetry.objects.create(station=station)
    resp = bearer(api_topology["admin"]).get(f"/api/v1/stations/{station.pk}/telemetry/")
    assert resp.data["metrics"] == []


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_contains_metric_fields(api_topology, bearer):
    resp = bearer(api_topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    paths = resp.data["paths"]
    assert "/api/v1/metrics/definitions/" in paths
    assert "/api/v1/metrics/definitions/{id}/" in paths
    assert "post" not in paths["/api/v1/metrics/definitions/"]
    assert "delete" not in paths["/api/v1/metrics/definitions/{id}/"]
    schemas = resp.data["components"]["schemas"]
    assert "metric_key" in schemas["AlertRule"]["properties"]
    tel = schemas["StationTelemetry"]["properties"]["metrics"]
    assert tel["type"] == "array"
    items = tel["items"]
    if "$ref" in items:
        items = schemas[items["$ref"].rsplit("/", 1)[-1]]
    assert {"key", "label", "unit", "avg", "ts"} <= set(items["properties"])


# ---------------------------------------------------------------------------
# review round 2: effective range (G), operator-only writes (K)
# ---------------------------------------------------------------------------


@pytest.fixture
def ranged_def(db):
    from apps.monitoring.models import MetricDefinition

    obj, _ = MetricDefinition.objects.update_or_create(
        key="pwr.12v.voltage_v",
        defaults={"label": "12V", "unit": "V", "min_value": 0.0, "max_value": 30.0},
    )
    return obj


def test_min_override_alone_above_agent_max_rejected(api_topology, bearer, ranged_def):
    resp = bearer(api_topology["admin"]).patch(
        _detail_url(ranged_def.pk), {"min_override": 40.0}, format="json"
    )
    assert resp.status_code == 400
    assert "min_override" in resp.data
    ranged_def.refresh_from_db()
    assert ranged_def.min_override is None


def test_max_override_alone_below_agent_min_rejected(api_topology, bearer, ranged_def):
    resp = bearer(api_topology["admin"]).patch(
        _detail_url(ranged_def.pk), {"max_override": -5.0}, format="json"
    )
    assert resp.status_code == 400
    assert "max_override" in resp.data


def test_one_sided_override_within_agent_range_ok(api_topology, bearer, ranged_def):
    resp = bearer(api_topology["admin"]).patch(
        _detail_url(ranged_def.pk), {"min_override": 9.5}, format="json"
    )
    assert resp.status_code == 200, resp.content


def test_serializer_update_writes_only_operator_fields(ranged_def):
    from apps.api.read_serializers import MetricDefinitionSerializer
    from apps.monitoring.models import MetricDefinition

    instance = MetricDefinition.objects.get(pk=ranged_def.pk)
    # A heartbeat updates the agent fields after the instance was fetched.
    MetricDefinition.objects.filter(pk=ranged_def.pk).update(label="new agent label", unit="mV")
    ser = MetricDefinitionSerializer(instance, data={"label_override": "Rail"}, partial=True)
    assert ser.is_valid(), ser.errors
    ser.save()
    ranged_def.refresh_from_db()
    assert ranged_def.label_override == "Rail"
    assert ranged_def.label == "new agent label"
    assert ranged_def.unit == "mV"
