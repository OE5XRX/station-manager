"""Heartbeat API contract for generic metrics (signed device requests)."""

import json
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client

from apps.monitoring.models import MetricDefinition
from apps.stations.models import StationTelemetry
from station_agent.metrics_contract import clean_descriptor, descriptors_hash
from tests.conftest import device_auth_headers

DESCS = [
    {
        "key": "pwr.12v.voltage_v",
        "label": "12 V rail voltage",
        "unit": "V",
        "group": "power",
        "precision": 2,
        "min": 0,
        "max": 30,
    }
]
HASH = descriptors_hash([clean_descriptor(d) for d in DESCS])


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def _post(station, private_key, **extra):
    body = {
        "hostname": "h",
        "os_version": "o",
        "uptime": 5.0,
        "module_versions": {},
        "ip_address": "10.0.0.2",
        "agent_version": "0.1.0",
        **extra,
    }
    body_bytes = json.dumps(body).encode()
    headers = device_auth_headers(private_key, station.id, body_bytes)
    return Client().post(
        "/api/v1/heartbeat/", data=body_bytes, content_type="application/json", **headers
    )


@pytest.mark.django_db
def test_unknown_hash_requests_descriptors(station_with_key):
    station, key = station_with_key
    resp = _post(station, key, metrics={"pwr.12v.voltage_v": 12.1}, metric_descriptors_hash=HASH)
    assert resp.status_code == 200
    assert resp.json()["metric_descriptors_required"] is True
    assert (
        StationTelemetry.objects.get(station=station).metrics["pwr.12v.voltage_v"]["avg"] == 12.1
    )


@pytest.mark.django_db
def test_list_sent_then_flag_absent(station_with_key):
    station, key = station_with_key
    resp = _post(
        station,
        key,
        metrics={"pwr.12v.voltage_v": {"min": 11.8, "max": 12.3, "avg": 12.1}},
        metric_descriptors_hash=HASH,
        metric_descriptors=DESCS,
    )
    assert resp.status_code == 200
    assert "metric_descriptors_required" not in resp.json()
    assert MetricDefinition.objects.get(key="pwr.12v.voltage_v").label == "12 V rail voltage"
    tel = StationTelemetry.objects.get(station=station)
    assert tel.metric_descriptors_hash == HASH

    cache.clear()  # throttle
    resp = _post(station, key, metrics={"pwr.12v.voltage_v": 12.0}, metric_descriptors_hash=HASH)
    assert resp.status_code == 200
    assert "metric_descriptors_required" not in resp.json()


@pytest.mark.django_db
def test_ingest_metrics_raises_still_200(station_with_key):
    station, key = station_with_key
    with patch(
        "apps.monitoring.metrics_ingest.ingest_metrics", side_effect=RuntimeError("boom")
    ) as mock_ingest:
        resp = _post(station, key, metrics={"a.val": 1}, metric_descriptors_hash="x")
    assert mock_ingest.called
    assert resp.status_code == 200
    assert "metric_descriptors_required" not in resp.json()


@pytest.mark.django_db
def test_garbage_metrics_still_200(station_with_key):
    station, key = station_with_key
    resp = _post(
        station,
        key,
        metrics="x",
        metric_descriptors="y",
        metric_descriptors_hash="",
    )
    assert resp.status_code == 200


@pytest.mark.django_db
def test_wrong_shape_values_dropped_others_stored(station_with_key):
    station, key = station_with_key
    resp = _post(
        station,
        key,
        metrics={"a.str": "x", "a.bool": True, "a.partial": {"min": 1}, "a.ok": 2},
    )
    assert resp.status_code == 200
    assert set(StationTelemetry.objects.get(station=station).metrics) == {"a.ok"}


@pytest.mark.django_db
def test_legacy_agent_inventory_fallback(station_with_key):
    station, key = station_with_key
    resp = _post(
        station,
        key,
        inventory={
            "cpu": {"temperature_c": 50.0},
            "ram": {"usage_percent": 30.0},
            "disk": [{"mount": "/", "usage_percent": 60.0}],
        },
    )
    assert resp.status_code == 200
    assert "metric_descriptors_required" not in resp.json()
    stored = StationTelemetry.objects.get(station=station).metrics
    assert set(stored) == {"cpu.temp_c", "mem.used_pct", "disk.root.used_pct"}


# review round 4: a malformed hash never 400s the heartbeat
@pytest.mark.django_db
@pytest.mark.parametrize("bad_hash", [None, 123, "a" * 65, ["x"]])
def test_malformed_descriptors_hash_still_200(station_with_key, bad_hash):
    station, key = station_with_key
    resp = _post(
        station, key, metrics={"pwr.12v.voltage_v": 12.1}, metric_descriptors_hash=bad_hash
    )
    assert resp.status_code == 200, resp.content
    # treated as "no hash": the value is still stored
    assert (
        StationTelemetry.objects.get(station=station).metrics["pwr.12v.voltage_v"]["avg"] == 12.1
    )
