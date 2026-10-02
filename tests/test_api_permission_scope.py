import pytest
from rest_framework.test import APIRequestFactory

from apps.api.permissions import TopologyScopedPermission
from tests.test_api_read_fixtures import topology  # noqa: F401,F811


@pytest.fixture
def perm():
    return TopologyScopedPermission()


class _View:
    pass


@pytest.mark.django_db
@pytest.mark.parametrize(
    "role,allowed",
    [
        ("admin", True),
        ("staff", True),
        ("region_mgr", True),
        ("station_user", True),
        ("applicant", False),
    ],
)
def test_has_permission_by_role(perm, topology, role, allowed):  # noqa: F811
    req = APIRequestFactory().get("/")
    req.user = topology[role]
    assert perm.has_permission(req, _View()) is allowed


@pytest.mark.django_db
def test_anonymous_denied(perm):
    from django.contrib.auth.models import AnonymousUser

    req = APIRequestFactory().get("/")
    req.user = AnonymousUser()
    assert perm.has_permission(req, _View()) is False


@pytest.mark.django_db
def test_devicekey_auth_denied(perm, topology):  # noqa: F811
    """A DeviceKey principal (no membership_level) must not pass."""
    from apps.api.models import DeviceKey

    req = APIRequestFactory().get("/")
    req.user = DeviceKey(station=topology["station_in"])  # not a real user
    assert perm.has_permission(req, _View()) is False


@pytest.mark.django_db
def test_accessible_deployments_scopes_by_results(topology):  # noqa: F811
    from apps.api.scoping import accessible_deployments
    from apps.deployments.models import Deployment, DeploymentResult
    from apps.images.models import ImageRelease

    rel = ImageRelease.objects.create(
        tag="v1",
        machine="qemux86-64",
        sha256="a" * 64,
        s3_key="images/v1/qemux86-64.wic.bz2",
        size_bytes=1000,
    )
    dep_all = Deployment.objects.create(image_release=rel, target_type="all")
    # touches only the out-of-scope station
    DeploymentResult.objects.create(deployment=dep_all, station=topology["station_out"])

    mgr = topology["region_mgr"]
    assert dep_all not in accessible_deployments(mgr)

    DeploymentResult.objects.create(deployment=dep_all, station=topology["station_in"])
    assert dep_all in accessible_deployments(mgr)
    assert dep_all in accessible_deployments(topology["admin"])
