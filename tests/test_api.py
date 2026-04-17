import json

import pytest
from django.urls import reverse

from tests.conftest import device_auth_headers


@pytest.mark.django_db
class TestHealthCheck:
    def test_health_check(self, client):
        response = client.get(reverse("api:health"))
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.django_db
class TestHeartbeat:
    def test_heartbeat_requires_auth(self, client):
        response = client.post(reverse("api:heartbeat"), content_type="application/json")
        assert response.status_code == 401

    def test_heartbeat_with_valid_signature(self, client, station_with_key):
        """A heartbeat signed with the station's Ed25519 key succeeds."""
        station, private_key = station_with_key
        body = json.dumps(
            {
                "hostname": "station-01",
                "os_version": "Yocto 4.0",
                "uptime": 3600.0,
                "ip_address": "192.168.1.100",
                "module_versions": {},
            }
        ).encode("utf-8")
        headers = device_auth_headers(private_key, station.pk, body)
        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 200

    def test_heartbeat_with_invalid_signature(self, client, station_with_key):
        """Wrong signature bytes should yield 401."""
        station, _ = station_with_key
        body = json.dumps({"hostname": "test"}).encode("utf-8")
        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {station.pk}",
            HTTP_X_DEVICE_SIGNATURE="bm90LXZhbGlk",  # "not-valid"
            HTTP_X_DEVICE_TIMESTAMP="9999999999.0",
        )
        assert response.status_code == 401

    def test_heartbeat_with_legacy_token_rejected(self, client, station_with_key):
        """Old ``Authorization: Device <token>`` headers must no longer work."""
        station, _ = station_with_key
        response = client.post(
            reverse("api:heartbeat"),
            data={"hostname": "test"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Device some-legacy-token",
        )
        assert response.status_code == 401
