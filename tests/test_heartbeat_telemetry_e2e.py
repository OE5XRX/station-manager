import json
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client

from apps.stations.models import StationTelemetry
from tests.conftest import device_auth_headers


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    # HeartbeatView has a ScopedRateThrottle (10/min) whose counter lives in the
    # default cache; clear it per-test so throttle state doesn't bleed across the suite.
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_heartbeat_persists_telemetry(station_with_key):
    station, private_key = station_with_key
    body = {
        "hostname": "h", "os_version": "o", "uptime": 5.0,
        "module_versions": {}, "ip_address": "10.0.0.2",
        "telemetry": {"boot": {"boot_id": "x1", "boot_count": 1, "reboot_reason": "clean"},
                      "slot": {"active_slot": "a", "image_version": "v2"}},
    }
    body_bytes = json.dumps(body).encode()
    headers = device_auth_headers(private_key, station.id, body_bytes)
    resp = Client().post("/api/v1/heartbeat/", data=body_bytes,
                         content_type="application/json", **headers)
    assert resp.status_code == 200
    tel = StationTelemetry.objects.get(station=station)
    assert tel.boot_id == "x1"
    assert tel.active_slot == "a"


@pytest.mark.django_db
def test_heartbeat_telemetry_ingest_raises_still_200(station_with_key):
    """Load-bearing guarantee: if ingest_telemetry raises, the heartbeat
    must still return 200 (telemetry failure never breaks liveness).

    The view imports ingest_telemetry from apps.stations.ingest at call
    time, so patch it at the source module.
    """
    station, private_key = station_with_key
    body = {
        "hostname": "h", "os_version": "o", "uptime": 5.0,
        "module_versions": {}, "ip_address": "10.0.0.2",
        "telemetry": {"boot": {"boot_id": "x1", "boot_count": 1, "reboot_reason": "clean"}},
    }
    body_bytes = json.dumps(body).encode()
    headers = device_auth_headers(private_key, station.id, body_bytes)
    with patch(
        "apps.stations.ingest.ingest_telemetry",
        side_effect=RuntimeError("boom"),
    ) as mock_ingest:
        resp = Client().post("/api/v1/heartbeat/", data=body_bytes,
                             content_type="application/json", **headers)
    assert mock_ingest.called
    assert resp.status_code == 200
    # The 200 did not depend on telemetry being persisted.
    assert not StationTelemetry.objects.filter(station=station).exists()


@pytest.mark.django_db
def test_heartbeat_malformed_telemetry_still_200(station_with_key):
    station, private_key = station_with_key
    body = {"hostname": "h", "os_version": "o", "uptime": 5.0,
            "module_versions": {}, "ip_address": "10.0.0.2",
            "telemetry": {"boot": "garbage"}}
    body_bytes = json.dumps(body).encode()
    headers = device_auth_headers(private_key, station.id, body_bytes)
    resp = Client().post("/api/v1/heartbeat/", data=body_bytes,
                         content_type="application/json", **headers)
    assert resp.status_code == 200
