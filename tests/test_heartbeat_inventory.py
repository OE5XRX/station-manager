import json

import pytest
from django.urls import reverse

from apps.stations.models import StationInventory
from tests.conftest import device_auth_headers


@pytest.mark.django_db
class TestHeartbeatWithInventory:
    def _heartbeat_payload(self, inventory=None):
        payload = {
            "hostname": "station-01",
            "os_version": "Yocto 4.0",
            "uptime": 3600.0,
            "ip_address": "192.168.1.100",
            "module_versions": {},
        }
        if inventory is not None:
            payload["inventory"] = inventory
        return payload

    def test_heartbeat_creates_inventory(self, client, station_with_key):
        """Heartbeat with inventory dict should create StationInventory."""
        station, private_key = station_with_key
        inventory_data = {
            "cpu": {"model": "ARM Cortex-A72", "cores": 4, "temperature_c": 55.0},
            "ram": {"total_mb": 4096, "usage_percent": 45.0},
            "disk": [{"mount": "/", "total_gb": 32, "usage_percent": 60.0}],
        }
        body = json.dumps(self._heartbeat_payload(inventory=inventory_data)).encode("utf-8")
        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        assert StationInventory.objects.filter(station=station).exists()
        inv = StationInventory.objects.get(station=station)
        assert inv.data["cpu"]["temperature_c"] == 55.0
        assert inv.data["ram"]["usage_percent"] == 45.0

    def test_heartbeat_updates_inventory(self, client, station_with_key):
        """Second heartbeat should update existing inventory, not create duplicate."""
        station, private_key = station_with_key

        # First heartbeat with inventory
        first_inventory = {"cpu": {"temperature_c": 50.0}}
        body1 = json.dumps(self._heartbeat_payload(inventory=first_inventory)).encode("utf-8")
        client.post(
            reverse("api:heartbeat"),
            data=body1,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body1),
        )
        assert StationInventory.objects.filter(station=station).count() == 1

        # Second heartbeat with updated inventory
        second_inventory = {"cpu": {"temperature_c": 75.0}}
        body2 = json.dumps(self._heartbeat_payload(inventory=second_inventory)).encode("utf-8")
        client.post(
            reverse("api:heartbeat"),
            data=body2,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body2),
        )
        assert StationInventory.objects.filter(station=station).count() == 1
        inv = StationInventory.objects.get(station=station)
        assert inv.data["cpu"]["temperature_c"] == 75.0

    def test_heartbeat_without_inventory(self, client, station_with_key):
        """Heartbeat without inventory should not create StationInventory."""
        station, private_key = station_with_key
        body = json.dumps(self._heartbeat_payload()).encode("utf-8")
        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        assert not StationInventory.objects.filter(station=station).exists()

    def test_heartbeat_updates_station_fields(self, client, station_with_key):
        """Heartbeat should update station os_version, ip, status, last_seen."""
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
        assert station.current_os_version == "Yocto 4.0"
        assert station.last_ip_address == "192.168.1.100"
        assert station.status == "online"
        assert station.last_seen is not None


@pytest.mark.django_db
class TestInventoryView:
    def test_inventory_view_requires_login(self, client, station):
        """Unauthenticated user should get 403 from DRF."""
        response = client.get(
            reverse("api:station_inventory", kwargs={"station_id": station.pk}),
        )
        assert response.status_code == 403

    def test_inventory_view_member_forbidden(self, client, member_user, station):
        """Member should get 403 on inventory view."""
        client.force_login(member_user)
        response = client.get(
            reverse("api:station_inventory", kwargs={"station_id": station.pk}),
        )
        assert response.status_code == 403

    def test_inventory_view_returns_data(self, client, admin_user, station):
        """Admin should get inventory data as JSON."""
        StationInventory.objects.create(
            station=station,
            data={"cpu": {"cores": 4}, "ram": {"total_mb": 2048}},
        )
        client.force_login(admin_user)
        response = client.get(
            reverse("api:station_inventory", kwargs={"station_id": station.pk}),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["station_id"] == station.pk
        assert data["data"]["cpu"]["cores"] == 4
        assert "updated_at" in data

    def test_inventory_view_no_inventory_returns_404(self, client, admin_user, station):
        """Station without inventory should return 404."""
        client.force_login(admin_user)
        response = client.get(
            reverse("api:station_inventory", kwargs={"station_id": station.pk}),
        )
        assert response.status_code == 404

    def test_inventory_view_operator_access(self, client, operator_user, station):
        """Operator should be able to view inventory."""
        StationInventory.objects.create(
            station=station,
            data={"cpu": {"cores": 2}},
        )
        client.force_login(operator_user)
        response = client.get(
            reverse("api:station_inventory", kwargs={"station_id": station.pk}),
        )
        assert response.status_code == 200
