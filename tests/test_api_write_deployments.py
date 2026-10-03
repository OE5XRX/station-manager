"""Tests for the Deployment create-trigger write endpoint (Phase 3, Task 7).

The endpoint is CREATE-ONLY — list/retrieve are inherited from the read
viewset; PATCH/PUT/DELETE return 405.  Authz: internal or region-manager of
the target station's region.  Client-supplied status is ignored; created_by
is always the token user; target_type is forced to STATION.
"""

import pytest
from django.urls import reverse

from apps.deployments.models import Deployment, DeploymentResult
from apps.stations.models import StationAuditLog


@pytest.mark.django_db
def test_deploy_trigger_creates_result_and_supersedes(api_topology, bearer, image_release):
    t = api_topology
    payload = {
        "image_release": image_release.pk,
        "target_station": t["station_in"].pk,
        "status": "completed",  # must be IGNORED
    }
    r = bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 201
    dep = Deployment.objects.latest("id")
    assert dep.status == Deployment.Status.IN_PROGRESS  # client 'status' ignored
    assert dep.created_by == t["region_mgr"]
    assert DeploymentResult.objects.filter(deployment=dep, station=t["station_in"]).exists()


@pytest.mark.django_db
def test_deploy_out_of_scope_station_403(api_topology, bearer, image_release):
    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_out"].pk}
    assert (
        bearer(t["region_mgr"])
        .post(reverse("api:deployment-list"), payload, format="json")
        .status_code
        == 403
    )


@pytest.mark.django_db
def test_deployment_no_delete_no_update(api_topology, bearer, deployment):
    t = api_topology
    url = reverse("api:deployment-detail", args=[deployment.pk])
    assert bearer(t["admin"]).delete(url).status_code == 405
    assert bearer(t["admin"]).patch(url, {"status": "cancelled"}, format="json").status_code == 405


@pytest.mark.django_db
def test_staff_can_trigger(api_topology, bearer, image_release):
    """Internal (staff) user may trigger for any station."""
    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_in"].pk}
    r = bearer(t["staff"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 201


@pytest.mark.django_db
def test_applicant_cannot_trigger(api_topology, bearer, image_release):
    """Applicant-level user is forbidden at the permission gate (403)."""
    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_in"].pk}
    r = bearer(t["applicant"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_anon_cannot_trigger(anon_client, api_topology, image_release):
    """Unauthenticated request is rejected with 401."""
    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_in"].pk}
    r = anon_client.post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 401


@pytest.mark.django_db
def test_deploy_trigger_audit_row_created(api_topology, bearer, image_release):
    """A FIRMWARE_UPDATE audit row referencing the station is created with
    'via API token' in the message."""
    t = api_topology
    station = t["station_in"]
    payload = {"image_release": image_release.pk, "target_station": station.pk}
    bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json")
    log = (
        StationAuditLog.objects.filter(
            station=station,
            event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
        )
        .order_by("-id")
        .first()
    )
    assert log is not None
    assert "via API token" in log.message


@pytest.mark.django_db
def test_deploy_trigger_target_type_forced_station(api_topology, bearer, image_release):
    """target_type is always STATION regardless of client input."""
    t = api_topology
    payload = {
        "image_release": image_release.pk,
        "target_station": t["station_in"].pk,
        "target_type": "all",  # must be ignored
    }
    r = bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 201
    dep = Deployment.objects.latest("id")
    assert dep.target_type == Deployment.TargetType.STATION


@pytest.mark.django_db
def test_deploy_trigger_result_is_pending(api_topology, bearer, image_release):
    """The DeploymentResult created starts as PENDING, not anything else."""
    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_in"].pk}
    bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json")
    dep = Deployment.objects.latest("id")
    result = DeploymentResult.objects.get(deployment=dep, station=t["station_in"])
    assert result.status == DeploymentResult.Status.PENDING
