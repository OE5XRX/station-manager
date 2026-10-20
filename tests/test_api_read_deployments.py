import itertools

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.test_api_read_fixtures import bearer, client_for, session, topology  # noqa: F401

_counter = itertools.count(1)


def _dep(topology, *stations):  # noqa: F811
    from apps.deployments.models import Deployment, DeploymentResult
    from apps.images.models import ImageRelease

    tag = f"v{next(_counter)}"
    rel = ImageRelease.objects.create(
        tag=tag, machine="qemux86-64", sha256="a" * 64, s3_key=f"images/{tag}.wic", size_bytes=1000
    )
    dep = Deployment.objects.create(
        image_tag=rel.tag, image_channel=rel.channel, target_type="all"
    )
    for station in stations:
        DeploymentResult.objects.create(deployment=dep, image_release=rel, station=station)
    return dep


@pytest.mark.django_db
def test_member_sees_all_deployments(topology):  # noqa: F811
    # No topology scoping: deployments.view (Applicant) shows every deployment.
    dep_out = _dep(topology, topology["station_out"])
    dep_in = _dep(topology, topology["station_in"])
    for role in ("region_mgr", "station_user", "applicant"):
        client = client_for(topology[role])
        ids = {d["id"] for d in client.get("/api/v1/deployments/").data["results"]}
        assert {dep_in.pk, dep_out.pk} <= ids


@pytest.mark.django_db
def test_deployment_results_not_scoped(topology):  # noqa: F811
    _dep(topology, topology["station_in"])
    _dep(topology, topology["station_out"])
    c = bearer(topology["region_mgr"])
    resp = c.get("/api/v1/deployment-results/")
    assert resp.status_code == 200
    stations = {r["station"] for r in resp.data["results"]}
    assert stations == {topology["station_in"].pk, topology["station_out"].pk}


@pytest.mark.django_db
def test_nested_results_show_all_stations(topology):  # noqa: F811
    # No topology scoping: the nested /results/ action lists every result,
    # the same for a region manager as for an admin.
    dep = _dep(topology, topology["station_in"], topology["station_out"])

    mgr_resp = bearer(topology["region_mgr"]).get(f"/api/v1/deployments/{dep.pk}/results/")
    assert mgr_resp.status_code == 200
    mgr_stations = {r["station"] for r in mgr_resp.data["results"]}
    assert mgr_stations == {topology["station_in"].pk, topology["station_out"].pk}

    admin_resp = bearer(topology["admin"]).get(f"/api/v1/deployments/{dep.pk}/results/")
    assert admin_resp.status_code == 200
    admin_stations = {r["station"] for r in admin_resp.data["results"]}
    assert admin_stations == {topology["station_in"].pk, topology["station_out"].pk}


@pytest.mark.django_db
def test_deployment_progress_field_present(topology):  # noqa: F811
    dep = _dep(topology, topology["station_in"])
    resp = bearer(topology["admin"]).get(f"/api/v1/deployments/{dep.pk}/")
    assert resp.status_code == 200 and "progress" in resp.data


@pytest.mark.django_db
def test_deployment_progress_counts_all_results(topology):  # noqa: F811
    """Progress counts every result regardless of the caller's assignments."""
    from apps.deployments.models import DeploymentResult

    dep = _dep(topology, topology["station_in"], topology["station_out"])
    # Set both results to SUCCESS so progress.completed reflects scope correctly.
    DeploymentResult.objects.filter(deployment=dep).update(status=DeploymentResult.Status.SUCCESS)

    mgr_resp = bearer(topology["region_mgr"]).get(f"/api/v1/deployments/{dep.pk}/")
    assert mgr_resp.status_code == 200
    mgr_progress = mgr_resp.data["progress"]
    assert mgr_progress["total"] == 2, "no topology scoping on progress"
    assert mgr_progress["completed"] == 2

    admin_resp = bearer(topology["admin"]).get(f"/api/v1/deployments/{dep.pk}/")
    assert admin_resp.status_code == 200
    admin_progress = admin_resp.data["progress"]
    assert admin_progress["total"] == 2, "admin sees all results"
    assert admin_progress["completed"] == 2


@pytest.mark.django_db
def test_device_deployment_check_not_shadowed_by_router(topology):  # noqa: F811
    # The automation router must not swallow the device /deployments/check/ path.
    resp = bearer(topology["admin"]).get("/api/v1/deployments/check/")
    # Device endpoint rejects bearer/user auth (device-signature only) → 401/403,
    # NOT a 404 "no Deployment matches pk=check" from the router.
    assert resp.status_code in (401, 403, 405)


# ---------------------------------------------------------------------------
# N+1 fix: bulk progress query tests
# ---------------------------------------------------------------------------


def _count_list_queries(client):
    """Return the number of DB queries used to GET /api/v1/deployments/."""
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get("/api/v1/deployments/")
        assert resp.status_code == 200
    return len(ctx)


@pytest.mark.django_db
def test_deployment_list_progress_query_count_constant(topology):  # noqa: F811
    """Progress bulk-query must not scale with page size.

    Create 2 deployments, measure query count → q2.
    Create 2 more (total 4), measure again → q4.
    Assert q2 == q4: the progress aggregation is O(1) queries.
    """
    from apps.deployments.models import DeploymentResult

    admin_client = bearer(topology["admin"])

    dep1 = _dep(topology, topology["station_in"])
    dep2 = _dep(topology, topology["station_out"])
    DeploymentResult.objects.filter(deployment=dep1).update(status=DeploymentResult.Status.SUCCESS)
    DeploymentResult.objects.filter(deployment=dep2).update(status=DeploymentResult.Status.FAILED)
    q2 = _count_list_queries(admin_client)

    dep3 = _dep(topology, topology["station_in"])
    dep4 = _dep(topology, topology["station_out"])
    DeploymentResult.objects.filter(deployment=dep3).update(status=DeploymentResult.Status.PENDING)
    DeploymentResult.objects.filter(deployment=dep4).update(
        status=DeploymentResult.Status.DOWNLOADING
    )
    q4 = _count_list_queries(admin_client)

    assert q2 == q4, (
        f"Query count grew from {q2} (2 deployments) to {q4} (4 deployments) — "
        "progress N+1 not fixed"
    )


@pytest.mark.django_db
def test_deployment_list_progress_counts_all_results(topology):  # noqa: F811
    """List-level progress matches the detail endpoint: no topology scoping.

    A target_type=all deployment with SUCCESS results on two stations:
    region_mgr and admin both see progress.total == 2.
    """
    from apps.deployments.models import DeploymentResult

    dep = _dep(topology, topology["station_in"], topology["station_out"])
    DeploymentResult.objects.filter(deployment=dep).update(status=DeploymentResult.Status.SUCCESS)

    mgr_resp = bearer(topology["region_mgr"]).get("/api/v1/deployments/")
    assert mgr_resp.status_code == 200
    mgr_results = mgr_resp.data["results"]
    assert len(mgr_results) == 1
    mgr_progress = mgr_results[0]["progress"]
    assert mgr_progress["total"] == 2, "no topology scoping on list progress"
    assert mgr_progress["completed"] == 2

    admin_resp = bearer(topology["admin"]).get("/api/v1/deployments/")
    assert admin_resp.status_code == 200
    admin_results = [r for r in admin_resp.data["results"] if r["id"] == dep.pk]
    assert len(admin_results) == 1
    admin_progress = admin_results[0]["progress"]
    assert admin_progress["total"] == 2, "admin list must see all results"
    assert admin_progress["completed"] == 2
