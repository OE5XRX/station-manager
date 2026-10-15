"""The user/automation API has no topology scoping (spec §3.3).

Every principal with ``stations.view`` / ``topology.view`` (Applicant and up)
sees every station and region, regardless of region-manager or station
assignments — exactly like the UI.
"""

import pytest

from apps.stations.models import Region, RegionAssignment, StationAssignment
from tests.test_api_read_fixtures import bearer, client_for


@pytest.fixture
def region(db):
    return Region.objects.create(name="Alpen", slug="alpen")


def _station_names(user):
    resp = client_for(user).get("/api/v1/stations/")
    assert resp.status_code == 200
    return {row["name"] for row in resp.data["results"]}


@pytest.mark.django_db
@pytest.mark.parametrize("fixture", ["admin_user", "staff_user", "member_user", "applicant_user"])
def test_every_level_sees_all_stations(request, fixture, station_factory):
    user = request.getfixturevalue(fixture)
    a = station_factory()
    b = station_factory()
    assert _station_names(user) == {a.name, b.name}


@pytest.mark.django_db
def test_region_manager_sees_stations_outside_region(member_user, region, station_factory):
    in_region = station_factory(region=region)
    outside = station_factory()
    RegionAssignment.objects.create(
        user=member_user, region=region, role="manager", assigned_by=member_user
    )
    assert _station_names(member_user) == {in_region.name, outside.name}


@pytest.mark.django_db
def test_region_manager_sees_all_regions(member_user, region):
    other = Region.objects.create(name="Flachland", slug="flachland")
    RegionAssignment.objects.create(
        user=member_user, region=region, role="manager", assigned_by=member_user
    )
    resp = bearer(member_user).get("/api/v1/regions/")
    assert resp.status_code == 200
    assert {r["id"] for r in resp.data["results"]} == {region.pk, other.pk}


@pytest.mark.django_db
def test_station_assigned_member_sees_unassigned_stations(member_user, station_factory):
    assigned = station_factory()
    other = station_factory()
    StationAssignment.objects.create(
        user=member_user, station=assigned, role="maintainer", assigned_by=member_user
    )
    assert _station_names(member_user) == {assigned.name, other.name}
    resp = bearer(member_user).get(f"/api/v1/stations/{other.pk}/")
    assert resp.status_code == 200
