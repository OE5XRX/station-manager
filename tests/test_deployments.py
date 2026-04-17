import json

import pytest
from django.urls import reverse

from apps.deployments.models import Deployment, DeploymentResult
from tests.conftest import device_auth_headers


@pytest.mark.django_db
class TestDeploymentCheck:
    def test_check_returns_pending_deployment(self, client, station_with_key, deployment_result):
        """Agent should receive firmware info for a pending deployment."""
        station, private_key = station_with_key
        result = deployment_result
        artifact = result.deployment.firmware_artifact

        response = client.get(
            reverse("api:deployment_check"),
            **device_auth_headers(private_key, station.pk),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["result_id"] == result.pk
        assert data["deployment_id"] == result.deployment_id
        assert data["firmware_name"] == artifact.name
        assert data["firmware_version"] == artifact.version
        assert data["checksum_sha256"] == artifact.checksum_sha256
        assert data["file_size"] == artifact.file_size

    def test_check_no_pending(self, client, station_with_key):
        """No pending deployment should return 204 No Content."""
        station, private_key = station_with_key
        response = client.get(
            reverse("api:deployment_check"),
            **device_auth_headers(private_key, station.pk),
        )
        assert response.status_code == 204

    def test_check_requires_device_auth(self, client):
        """Request without auth should return 401."""
        response = client.get(reverse("api:deployment_check"))
        assert response.status_code == 401


@pytest.mark.django_db
class TestDeploymentStatusUpdate:
    def test_status_update_downloading(self, client, station_with_key, deployment_result):
        """Status should transition to downloading."""
        station, private_key = station_with_key
        body = json.dumps({"status": "downloading"}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_status_update", kwargs={"pk": deployment_result.pk}),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.DOWNLOADING

    def test_status_update_sets_started_at(self, client, station_with_key, deployment_result):
        """First non-pending status should set started_at."""
        station, private_key = station_with_key
        assert deployment_result.started_at is None

        body = json.dumps({"status": "downloading"}).encode("utf-8")
        client.post(
            reverse("api:deployment_status_update", kwargs={"pk": deployment_result.pk}),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        deployment_result.refresh_from_db()
        assert deployment_result.started_at is not None

    def test_status_update_failed_sets_completed(
        self, client, station_with_key, deployment_result
    ):
        """Terminal status (failed) should set completed_at."""
        station, private_key = station_with_key
        body = json.dumps({"status": "failed", "error_message": "Checksum mismatch"}).encode(
            "utf-8"
        )
        client.post(
            reverse("api:deployment_status_update", kwargs={"pk": deployment_result.pk}),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.FAILED
        assert deployment_result.completed_at is not None
        assert deployment_result.error_message == "Checksum mismatch"


@pytest.mark.django_db
class TestDeploymentCommit:
    def test_commit_marks_success(self, client, station_with_key, deployment_result):
        """Agent commit should mark result as SUCCESS."""
        station, private_key = station_with_key
        # Move result to REBOOTING first (commit looks for rebooting/verifying/installing)
        deployment_result.status = DeploymentResult.Status.REBOOTING
        deployment_result.save(update_fields=["status"])

        body = json.dumps({"version": "1.0.0"}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_commit"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.SUCCESS
        assert deployment_result.completed_at is not None
        assert deployment_result.new_version == "1.0.0"

    def test_commit_completes_deployment(self, client, station_with_key, deployment_result):
        """When all results are done, deployment status should update."""
        station, private_key = station_with_key
        deployment_result.status = DeploymentResult.Status.REBOOTING
        deployment_result.save(update_fields=["status"])

        body = json.dumps({"version": "1.0.0"}).encode("utf-8")
        client.post(
            reverse("api:deployment_commit"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        deployment = deployment_result.deployment
        deployment.refresh_from_db()
        assert deployment.status == Deployment.Status.COMPLETED


@pytest.mark.django_db
class TestDeploymentWebViews:
    def test_create_deployment_creates_results(
        self, client, operator_user, firmware_artifact, station
    ):
        """Creating a deployment should create DeploymentResult per target station."""
        client.force_login(operator_user)
        response = client.post(
            reverse("deployments:deployment_create"),
            data={
                "firmware_artifact": firmware_artifact.pk,
                "target_type": Deployment.TargetType.STATION,
                "target_station": station.pk,
                "strategy": Deployment.Strategy.IMMEDIATE,
                "phase_config": "{}",
            },
        )
        assert response.status_code == 302
        dep = Deployment.objects.latest("created_at")
        assert dep.results.count() == 1
        assert dep.results.first().station == station
        assert dep.status == Deployment.Status.IN_PROGRESS

    def test_cancel_deployment(self, client, operator_user, deployment, deployment_result):
        """Cancelling should set deployment and pending results to cancelled."""
        client.force_login(operator_user)
        response = client.post(
            reverse("deployments:deployment_cancel", kwargs={"pk": deployment.pk}),
        )
        assert response.status_code == 302
        deployment.refresh_from_db()
        assert deployment.status == Deployment.Status.CANCELLED
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.CANCELLED

    def test_deployment_list_requires_operator(self, client, member_user):
        """Member should get 403 on deployment list."""
        client.force_login(member_user)
        response = client.get(reverse("deployments:deployment_list"))
        assert response.status_code == 403

    def test_deployment_detail_shows_progress(
        self, client, operator_user, deployment, deployment_result
    ):
        """Detail page should render for operator."""
        client.force_login(operator_user)
        response = client.get(
            reverse("deployments:deployment_detail", kwargs={"pk": deployment.pk}),
        )
        assert response.status_code == 200
