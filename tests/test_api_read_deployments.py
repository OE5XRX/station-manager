import itertools

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401

_counter = itertools.count(1)


def _dep(topology, *stations):  # noqa: F811
    from apps.deployments.models import Deployment, DeploymentResult
    from apps.images.models import ImageRelease

    tag = f"v{next(_counter)}"
    rel = ImageRelease.objects.create(
        tag=tag, machine="qemux86-64", sha256="a" * 64, s3_key=f"images/{tag}.wic", size_bytes=1000
    )
    dep = Deployment.objects.create(image_release=rel, target_type="all")
    for station in stations:
        DeploymentResult.objects.create(deployment=dep, station=station)
    return dep


@pytest.mark.django_db
def test_region_mgr_sees_deployment_only_if_touches_their_station(topology):  # noqa: F811
    dep_out = _dep(topology, topology["station_out"])
    c = bearer(topology["region_mgr"])
    assert dep_out.pk not in {d["id"] for d in c.get("/api/v1/deployments/").data["results"]}
    dep_in = _dep(topology, topology["station_in"])
    ids = {d["id"] for d in c.get("/api/v1/deployments/").data["results"]}
    assert dep_in.pk in ids
    # An in-scope result must not retroactively expose out-of-scope deployments.
    assert dep_out.pk not in ids


@pytest.mark.django_db
def test_deployment_results_scoped(topology):  # noqa: F811
    _dep(topology, topology["station_in"])
    _dep(topology, topology["station_out"])
    c = bearer(topology["region_mgr"])
    resp = c.get("/api/v1/deployment-results/")
    assert resp.status_code == 200
    stations = {r["station"] for r in resp.data["results"]}
    assert topology["station_out"].pk not in stations


@pytest.mark.django_db
def test_nested_results_scoped_to_accessible_stations(topology):  # noqa: F811
    # A broad (target_type=all) deployment touching both an in-scope and an
    # out-of-scope station must not leak the out-of-scope result through the
    # nested /results/ action.
    dep = _dep(topology, topology["station_in"], topology["station_out"])

    mgr_resp = bearer(topology["region_mgr"]).get(f"/api/v1/deployments/{dep.pk}/results/")
    assert mgr_resp.status_code == 200
    mgr_stations = {r["station"] for r in mgr_resp.data["results"]}
    assert mgr_stations == {topology["station_in"].pk}
    assert topology["station_out"].pk not in mgr_stations

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
def test_deployment_progress_scoped_to_accessible_stations(topology):  # noqa: F811
    """region_mgr sees only their station's result in progress; admin sees all."""
    from apps.deployments.models import DeploymentResult

    dep = _dep(topology, topology["station_in"], topology["station_out"])
    # Set both results to SUCCESS so progress.completed reflects scope correctly.
    DeploymentResult.objects.filter(deployment=dep).update(status=DeploymentResult.Status.SUCCESS)

    mgr_resp = bearer(topology["region_mgr"]).get(f"/api/v1/deployments/{dep.pk}/")
    assert mgr_resp.status_code == 200
    mgr_progress = mgr_resp.data["progress"]
    assert mgr_progress["total"] == 1, "region_mgr must only count in-scope result"
    assert mgr_progress["completed"] == 1

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
def test_deployment_list_progress_scoped_to_accessible_stations(topology):  # noqa: F811
    """List-level progress must apply the same scope as the detail endpoint.

    A target_type=all deployment with one in-scope SUCCESS result and one
    out-of-scope SUCCESS result:
    - region_mgr list sees progress.total == 1
    - admin list sees progress.total == 2
    """
    from apps.deployments.models import DeploymentResult

    dep = _dep(topology, topology["station_in"], topology["station_out"])
    DeploymentResult.objects.filter(deployment=dep).update(status=DeploymentResult.Status.SUCCESS)

    mgr_resp = bearer(topology["region_mgr"]).get("/api/v1/deployments/")
    assert mgr_resp.status_code == 200
    mgr_results = mgr_resp.data["results"]
    assert len(mgr_results) == 1
    mgr_progress = mgr_results[0]["progress"]
    assert mgr_progress["total"] == 1, "region_mgr list must only count in-scope result"
    assert mgr_progress["completed"] == 1

    admin_resp = bearer(topology["admin"]).get("/api/v1/deployments/")
    assert admin_resp.status_code == 200
    admin_results = [r for r in admin_resp.data["results"] if r["id"] == dep.pk]
    assert len(admin_results) == 1
    admin_progress = admin_results[0]["progress"]
    assert admin_progress["total"] == 2, "admin list must see all results"
    assert admin_progress["completed"] == 2
