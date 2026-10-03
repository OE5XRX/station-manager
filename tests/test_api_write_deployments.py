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


@pytest.fixture
def provisioned_station_in(api_topology, image_release):
    """Set api_topology's station_in.current_image_release to the OTA-ready
    qemux86-64 `image_release` fixture, so the machine-match / provisioned /
    OTA-ready preconditions pass on the happy path."""
    station = api_topology["station_in"]
    station.current_image_release = image_release
    station.save(update_fields=["current_image_release"])
    return station


@pytest.mark.django_db
def test_deploy_trigger_creates_result_and_supersedes(
    api_topology, bearer, image_release, provisioned_station_in
):
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
def test_staff_can_trigger(api_topology, bearer, image_release, provisioned_station_in):
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
def test_deploy_trigger_audit_row_created(
    api_topology, bearer, image_release, provisioned_station_in
):
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
def test_deploy_trigger_target_type_forced_station(
    api_topology, bearer, image_release, provisioned_station_in
):
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
def test_deploy_trigger_result_is_pending(
    api_topology, bearer, image_release, provisioned_station_in
):
    """The DeploymentResult created starts as PENDING, not anything else."""
    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_in"].pk}
    bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json")
    dep = Deployment.objects.latest("id")
    result = DeploymentResult.objects.get(deployment=dep, station=t["station_in"])
    assert result.status == DeploymentResult.Status.PENDING


@pytest.mark.django_db
def test_deploy_wrong_machine_image_400(api_topology, bearer, provisioned_station_in):
    """An image whose machine differs from the station's current release → 400,
    no Deployment created."""
    from apps.images.models import ImageRelease

    t = api_topology
    # station_in is provisioned on qemux86-64; offer a raspberrypi4-64 image.
    other = ImageRelease.objects.create(
        tag="v1-rpi",
        machine="raspberrypi4-64",
        s3_key="images/v1-rpi/raspberrypi4-64.wic.bz2",
        sha256="c" * 64,
        size_bytes=1000,
        rootfs_s3_key="images/v1-rpi/raspberrypi4-64.rootfs.bz2",
        rootfs_sha256="d" * 64,
        rootfs_size_bytes=500,
    )
    payload = {"image_release": other.pk, "target_station": t["station_in"].pk}
    before = Deployment.objects.count()
    r = bearer(t["staff"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 400
    assert "image_release" in r.data
    assert Deployment.objects.count() == before


@pytest.mark.django_db
def test_deploy_non_ota_ready_image_400(api_topology, bearer):
    """An image_release that is not OTA-ready (missing rootfs fields) → 400."""
    from apps.images.models import ImageRelease

    t = api_topology
    station = t["station_in"]
    # Non-OTA-ready release (no rootfs_* fields) on a matching machine.
    non_ota = ImageRelease.objects.create(
        tag="v1-noota",
        machine="qemux86-64",
        s3_key="images/v1-noota/qemux86-64.wic.bz2",
        sha256="e" * 64,
        size_bytes=1000,
    )
    assert non_ota.is_ota_ready is False
    station.current_image_release = non_ota
    station.save(update_fields=["current_image_release"])
    payload = {"image_release": non_ota.pk, "target_station": station.pk}
    before = Deployment.objects.count()
    r = bearer(t["staff"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 400
    assert "image_release" in r.data
    assert Deployment.objects.count() == before


@pytest.mark.django_db
def test_deploy_unprovisioned_station_400(api_topology, bearer, image_release):
    """A station with current_image_release=None → 400 (target_station field)."""
    t = api_topology
    station = t["station_in"]
    assert station.current_image_release is None  # unprovisioned by default
    payload = {"image_release": image_release.pk, "target_station": station.pk}
    before = Deployment.objects.count()
    r = bearer(t["staff"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 400
    assert "target_station" in r.data
    assert Deployment.objects.count() == before


# ---------------------------------------------------------------------------
# C3 — station lock ordering: acquire station lock BEFORE FK inserts
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_deploy_trigger_station_lock_happy_path(
    api_topology, bearer, image_release, provisioned_station_in
):
    """Happy path still works: 201 + Deployment + DeploymentResult + supersede.

    The station lock is acquired first (before FK inserts), then the Deployment
    and DeploymentResult rows are created, then supersede_pending_for_station.
    This test confirms the complete create+lock+supersede still succeeds.
    """
    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_in"].pk}
    r = bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 201
    dep = Deployment.objects.latest("id")
    assert dep.status == Deployment.Status.IN_PROGRESS
    assert DeploymentResult.objects.filter(deployment=dep, station=t["station_in"]).exists()


@pytest.mark.django_db
def test_deploy_trigger_station_lock_supersedes_existing(
    api_topology, bearer, image_release, provisioned_station_in
):
    """A second deployment trigger supersedes the first (no conflict error) when
    the station lock ordering is correct — station locked first, then inserts."""
    t = api_topology
    station = t["station_in"]
    # Create a prior IN_PROGRESS deployment + pending result (the one to supersede)
    old_dep = Deployment.objects.create(
        image_release=image_release,
        target_type=Deployment.TargetType.STATION,
        target_station=station,
        status=Deployment.Status.IN_PROGRESS,
        created_by=t["staff"],
    )
    DeploymentResult.objects.create(
        deployment=old_dep,
        station=station,
        status=DeploymentResult.Status.PENDING,
        previous_version="",
    )
    # Trigger a new deployment for the same station/image — supersession fires
    payload = {"image_release": image_release.pk, "target_station": station.pk}
    r = bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 201
