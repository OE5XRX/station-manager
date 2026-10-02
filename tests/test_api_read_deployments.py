import itertools

import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401

_counter = itertools.count(1)


def _dep(topology, station):  # noqa: F811
    from apps.deployments.models import Deployment, DeploymentResult
    from apps.images.models import ImageRelease

    tag = f"v{next(_counter)}"
    rel = ImageRelease.objects.create(
        tag=tag, machine="qemux86-64", sha256="a" * 64, s3_key=f"images/{tag}.wic", size_bytes=1000
    )
    dep = Deployment.objects.create(image_release=rel, target_type="all")
    DeploymentResult.objects.create(deployment=dep, station=station)
    return dep


@pytest.mark.django_db
def test_region_mgr_sees_deployment_only_if_touches_their_station(topology):  # noqa: F811
    dep_out = _dep(topology, topology["station_out"])
    c = bearer(topology["region_mgr"])
    assert dep_out.pk not in {d["id"] for d in c.get("/api/v1/deployments/").data["results"]}
    dep_in = _dep(topology, topology["station_in"])
    assert dep_in.pk in {d["id"] for d in c.get("/api/v1/deployments/").data["results"]}


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
def test_deployment_progress_field_present(topology):  # noqa: F811
    dep = _dep(topology, topology["station_in"])
    resp = bearer(topology["admin"]).get(f"/api/v1/deployments/{dep.pk}/")
    assert resp.status_code == 200 and "progress" in resp.data


@pytest.mark.django_db
def test_device_deployment_check_not_shadowed_by_router(topology):  # noqa: F811
    # The automation router must not swallow the device /deployments/check/ path.
    resp = bearer(topology["admin"]).get("/api/v1/deployments/check/")
    # Device endpoint rejects bearer/user auth (device-signature only) → 401/403,
    # NOT a 404 "no Deployment matches pk=check" from the router.
    assert resp.status_code in (401, 403, 405)
