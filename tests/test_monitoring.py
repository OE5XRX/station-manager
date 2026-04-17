from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.deployments.models import DeploymentResult
from apps.monitoring.engine import (
    _check_cpu_temperature,
    _check_disk_usage,
    _check_ota_failed,
    _check_ram_usage,
    _check_station_offline,
    check_alerts,
)
from apps.stations.models import StationInventory


@pytest.mark.django_db
class TestCheckStationOffline:
    def test_station_offline_alert(self, station, offline_alert_rule):
        """Station with last_seen > 5 min ago triggers alert."""
        station.last_seen = timezone.now() - timedelta(minutes=10)
        station.status = "online"
        station.save(update_fields=["last_seen", "status"])

        alerts = _check_station_offline()
        assert len(alerts) == 1
        assert "offline" in alerts[0].title.lower()
        assert alerts[0].station == station

    def test_station_offline_no_duplicate(self, station, offline_alert_rule):
        """Same alert should not be created twice."""
        station.last_seen = timezone.now() - timedelta(minutes=10)
        station.status = "online"
        station.save(update_fields=["last_seen", "status"])

        alerts_first = _check_station_offline()
        assert len(alerts_first) == 1

        alerts_second = _check_station_offline()
        assert len(alerts_second) == 0

    def test_station_online_auto_resolves(self, station, offline_alert_rule):
        """When station comes back online, alert should be auto-resolved."""
        station.last_seen = timezone.now() - timedelta(minutes=10)
        station.status = "online"
        station.save(update_fields=["last_seen", "status"])

        alerts = _check_station_offline()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.is_resolved is False

        # Station comes back online
        station.last_seen = timezone.now()
        station.save(update_fields=["last_seen"])

        _check_station_offline()
        alert.refresh_from_db()
        assert alert.is_resolved is True
        assert alert.resolved_at is not None


@pytest.mark.django_db
class TestCheckCpuTemperature:
    def test_cpu_temperature_alert(self, station, cpu_temp_alert_rule):
        """Inventory with temp > threshold triggers alert."""
        StationInventory.objects.create(
            station=station,
            data={"cpu": {"temperature_c": 85.0}},
        )
        alerts = _check_cpu_temperature()
        assert len(alerts) == 1
        assert "temperature" in alerts[0].title.lower()

    def test_cpu_temperature_below_threshold_no_alert(self, station, cpu_temp_alert_rule):
        """Temp below threshold should not trigger alert."""
        StationInventory.objects.create(
            station=station,
            data={"cpu": {"temperature_c": 50.0}},
        )
        alerts = _check_cpu_temperature()
        assert len(alerts) == 0


@pytest.mark.django_db
class TestCheckDiskUsage:
    def test_disk_usage_alert(self, station, disk_warning_alert_rule):
        """Disk > 90% triggers warning."""
        StationInventory.objects.create(
            station=station,
            data={"disk": [{"mount": "/", "usage_percent": 95.0}]},
        )
        alerts = _check_disk_usage()
        assert len(alerts) == 1
        assert "disk" in alerts[0].title.lower()

    def test_disk_usage_below_threshold_no_alert(self, station, disk_warning_alert_rule):
        """Disk below threshold should not trigger alert."""
        StationInventory.objects.create(
            station=station,
            data={"disk": [{"mount": "/", "usage_percent": 50.0}]},
        )
        alerts = _check_disk_usage()
        assert len(alerts) == 0


@pytest.mark.django_db
class TestCheckRamUsage:
    def test_ram_usage_alert(self, station, ram_critical_alert_rule):
        """RAM > 90% triggers alert."""
        StationInventory.objects.create(
            station=station,
            data={"ram": {"usage_percent": 95.0}},
        )
        alerts = _check_ram_usage()
        assert len(alerts) == 1
        assert "ram" in alerts[0].title.lower()

    def test_ram_usage_below_threshold_no_alert(self, station, ram_critical_alert_rule):
        """RAM below threshold should not trigger alert."""
        StationInventory.objects.create(
            station=station,
            data={"ram": {"usage_percent": 50.0}},
        )
        alerts = _check_ram_usage()
        assert len(alerts) == 0


@pytest.mark.django_db
class TestCheckOtaFailed:
    def test_ota_failed_alert(self, station, ota_failed_alert_rule, deployment, deployment_result):
        """Failed deployment result triggers alert."""
        deployment_result.status = DeploymentResult.Status.FAILED
        deployment_result.completed_at = timezone.now()
        deployment_result.error_message = "Boot failed"
        deployment_result.save(update_fields=["status", "completed_at", "error_message"])

        alerts = _check_ota_failed()
        assert len(alerts) == 1
        assert "ota" in alerts[0].title.lower()
        assert "failed" in alerts[0].title.lower()

    def test_ota_failed_old_result_not_alerted(
        self, station, ota_failed_alert_rule, deployment, deployment_result
    ):
        """Failed result older than 5 min window should not trigger alert."""
        deployment_result.status = DeploymentResult.Status.FAILED
        deployment_result.completed_at = timezone.now() - timedelta(minutes=10)
        deployment_result.save(update_fields=["status", "completed_at"])

        alerts = _check_ota_failed()
        assert len(alerts) == 0


@pytest.mark.django_db
class TestCheckAlertsIntegration:
    def test_check_alerts_runs_all_checks(self, station, offline_alert_rule):
        """check_alerts() should run all checks and return new alerts."""
        station.last_seen = timezone.now() - timedelta(minutes=10)
        station.status = "online"
        station.save(update_fields=["last_seen", "status"])

        new_alerts = check_alerts()
        assert len(new_alerts) >= 1


@pytest.mark.django_db
class TestAlertViews:
    def test_alert_list_requires_admin(self, client, member_user):
        """Member should get 403 on alert list."""
        client.force_login(member_user)
        response = client.get(reverse("monitoring:alert_list"))
        assert response.status_code == 403

    def test_alert_list_admin_access(self, client, admin_user, alert):
        """Admin should be able to view alert list."""
        client.force_login(admin_user)
        response = client.get(reverse("monitoring:alert_list"))
        assert response.status_code == 200

    def test_alert_acknowledge(self, client, operator_user, alert):
        """POST acknowledge should set is_acknowledged."""
        client.force_login(operator_user)
        response = client.post(
            reverse("monitoring:alert_acknowledge", kwargs={"pk": alert.pk}),
        )
        assert response.status_code == 200
        alert.refresh_from_db()
        assert alert.is_acknowledged is True
        assert alert.acknowledged_by == operator_user
        assert alert.acknowledged_at is not None

    def test_alert_resolve(self, client, operator_user, alert):
        """POST resolve should set is_resolved."""
        client.force_login(operator_user)
        response = client.post(
            reverse("monitoring:alert_resolve", kwargs={"pk": alert.pk}),
        )
        assert response.status_code == 200
        alert.refresh_from_db()
        assert alert.is_resolved is True
        assert alert.resolved_at is not None

    def test_alert_count_returns_json(self, client, admin_user, alert):
        """Alert count endpoint should return JSON with counts."""
        client.force_login(admin_user)
        response = client.get(reverse("monitoring:alert_count"))
        assert response.status_code == 200
        data = response.json()
        assert "total" in data
        assert "critical" in data
        assert "warning" in data
        assert data["total"] >= 1

    def test_alert_count_requires_login(self, client):
        """Unauthenticated user should be redirected."""
        response = client.get(reverse("monitoring:alert_count"))
        assert response.status_code == 302

    def test_alert_settings_requires_admin(self, client, operator_user):
        """Operator should get 403 on alert settings (admin only)."""
        client.force_login(operator_user)
        response = client.get(reverse("monitoring:alert_settings"))
        assert response.status_code == 403

    def test_alert_settings_admin_access(self, client, admin_user):
        """Admin should be able to view alert settings."""
        client.force_login(admin_user)
        response = client.get(reverse("monitoring:alert_settings"))
        assert response.status_code == 200
