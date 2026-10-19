import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.stations.models import Station
from tests.conftest import device_auth_headers


def _payload(**extra):
    return {
        "hostname": "station-01",
        "os_version": "Yocto 4.0",
        "uptime": 3600.0,
        "ip_address": "192.168.1.100",
        "module_versions": {},
        **extra,
    }


@pytest.mark.django_db
class TestHeartbeatCapabilities:
    @pytest.fixture(autouse=True)
    def _clear_throttle_bucket(self):
        cache.clear()

    def _post(self, client, station, key, payload):
        body = json.dumps(payload).encode("utf-8")
        return client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            **device_auth_headers(key, station.pk, body),
        )

    def test_heartbeat_stores_capabilities(self, client, station_with_key):
        station, key = station_with_key
        caps = {"module_ota": {"backends": ["dfu"]}}
        resp = self._post(client, station, key, _payload(capabilities=caps))
        assert resp.status_code == 200
        station.refresh_from_db()
        assert station.agent_capabilities == caps

    def test_heartbeat_without_capabilities_clears(self, client, station_with_key):
        station, key = station_with_key
        station.agent_capabilities = {"module_ota": {"backends": ["dfu"]}}
        station.save(update_fields=["agent_capabilities"])
        resp = self._post(client, station, key, _payload())
        assert resp.status_code == 200
        station.refresh_from_db()
        assert station.agent_capabilities == {}

    def test_capabilities_size_bounded(self, client, station_with_key):
        station, key = station_with_key
        caps = {"module_ota": {"backends": ["dfu"]}, "junk": "x" * 5000}
        resp = self._post(client, station, key, _payload(capabilities=caps))
        assert resp.status_code == 400


def test_module_ota_backends_filters_garbage():
    s = Station(agent_capabilities={"module_ota": {"backends": ["dfu", "x", 3]}})
    assert s.module_ota_backends() == {"dfu"}
    assert Station(agent_capabilities={"module_ota": "bad"}).module_ota_backends() == set()
    assert (
        Station(agent_capabilities={"module_ota": {"backends": "dfu"}}).module_ota_backends()
        == set()
    )
    assert Station(agent_capabilities=["x"]).module_ota_backends() == set()
    assert Station().module_ota_backends() == set()
