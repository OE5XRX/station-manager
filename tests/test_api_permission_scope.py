"""Unit + API tests for the DRF ``AreaPermission`` gate on the automation API.

Replaces the old ``TopologyScopedPermission`` membership gate: safe methods
need ``<area>.view``, writes ``<area>.manage``; non-user principals are
refused without an audit row.
"""

import pytest
from rest_framework.test import APIRequestFactory

from apps.accounts.models import AccountAuditLog
from apps.accounts.permissions import AreaDenied, AreaPermission
from tests.test_api_read_fixtures import bearer, client_for, session, topology  # noqa: F401


@pytest.fixture
def perm():
    return AreaPermission()


class _StationsView:
    area = "stations"
    verb = None
    verbs = None


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["admin", "staff", "region_mgr", "station_user", "applicant"])
def test_read_allowed_for_every_role(perm, topology, role):  # noqa: F811
    req = APIRequestFactory().get("/")
    req.user = topology[role]
    assert perm.has_permission(req, _StationsView()) is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    "role,allowed",
    [
        ("admin", True),
        ("staff", True),
        ("region_mgr", False),
        ("station_user", False),
        ("applicant", False),
    ],
)
def test_write_needs_manage(perm, topology, role, allowed):  # noqa: F811
    req = APIRequestFactory().post("/")
    req.user = topology[role]
    if allowed:
        assert perm.has_permission(req, _StationsView()) is True
    else:
        with pytest.raises(AreaDenied):
            perm.has_permission(req, _StationsView())


@pytest.mark.django_db
def test_anonymous_denied(perm):
    from django.contrib.auth.models import AnonymousUser

    req = APIRequestFactory().get("/")
    req.user = AnonymousUser()
    assert perm.has_permission(req, _StationsView()) is False


@pytest.mark.django_db
def test_devicekey_principal_denied_without_audit(perm, topology):  # noqa: F811
    """A DeviceKey principal (no membership_level) must not pass and is not audited."""
    from apps.api.models import DeviceKey

    req = APIRequestFactory().post("/")
    req.user = DeviceKey(station=topology["station_in"])  # not a real user
    assert perm.has_permission(req, _StationsView()) is False
    assert not AccountAuditLog.objects.filter(event_type="permission_denied").exists()


@pytest.mark.django_db
def test_deployments_not_scoped_by_results(topology):  # noqa: F811
    """A deployment touching only a station outside the region manager's region
    is still listed for them (no topology scoping)."""
    from apps.deployments.models import Deployment, DeploymentResult
    from apps.images.models import ImageRelease

    rel = ImageRelease.objects.create(
        tag="v1",
        machine="qemux86-64",
        sha256="a" * 64,
        s3_key="images/v1/qemux86-64.wic.bz2",
        size_bytes=1000,
    )
    dep_all = Deployment.objects.create(
        image_tag=rel.tag, image_channel=rel.channel, target_type="all"
    )
    DeploymentResult.objects.create(
        deployment=dep_all, image_release=rel, station=topology["station_out"]
    )

    for role in ("region_mgr", "station_user", "applicant", "admin"):
        client = client_for(topology[role])
        resp = client.get("/api/v1/deployments/")
        assert resp.status_code == 200
        assert dep_all.pk in {d["id"] for d in resp.data["results"]}
