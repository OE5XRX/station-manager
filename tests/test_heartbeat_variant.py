import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.api.serializers import HeartbeatSerializer
from tests.conftest import device_auth_headers


def test_serializer_accepts_optional_image_variant():
    base = dict(hostname="h", os_version="o", uptime=1.0, module_versions={}, ip_address="10.0.0.1")
    s = HeartbeatSerializer(data={**base, "image_variant": "dev"})
    assert s.is_valid(), s.errors
    assert s.validated_data["image_variant"] == "dev"

    s2 = HeartbeatSerializer(data=base)
    assert s2.is_valid(), s2.errors
    assert s2.validated_data["image_variant"] == ""


@pytest.mark.django_db
class TestHeartbeatPersistsImageVariant:
    @pytest.fixture(autouse=True)
    def _clear_throttle_bucket(self):
        # The heartbeat endpoint uses a ScopedRateThrottle ("heartbeat", 10/min)
        # whose counter lives in the shared Django cache, which pytest does not
        # reset between tests. Earlier heartbeat tests can exhaust the bucket and
        # cause 429s here. Clear it before each test to make these tests
        # throttle-independent.
        cache.clear()

    def _heartbeat_payload(self, image_variant=None):
        payload = {
            "hostname": "station-01",
            "os_version": "Yocto 4.0",
            "uptime": 3600.0,
            "ip_address": "192.168.1.100",
            "module_versions": {},
        }
        if image_variant is not None:
            payload["image_variant"] = image_variant
        return payload

    def test_heartbeat_with_image_variant_persists(self, client, station_with_key):
        """Heartbeat carrying image_variant='dev' should persist it on the station."""
        station, private_key = station_with_key
        body = json.dumps(self._heartbeat_payload(image_variant="dev")).encode("utf-8")
        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        station.refresh_from_db()
        assert station.current_image_variant == "dev"

    def test_heartbeat_without_image_variant_sets_empty(self, client, station_with_key):
        """Heartbeat without image_variant should leave current_image_variant as ''."""
        station, private_key = station_with_key
        body = json.dumps(self._heartbeat_payload()).encode("utf-8")
        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        station.refresh_from_db()
        assert station.current_image_variant == ""
