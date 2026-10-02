import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401


@pytest.mark.django_db
def test_station_assignment_scope(topology):  # noqa: F811
    # station_user has an assignment on station_in; must see it, not others'
    from apps.stations.models import StationAssignment

    StationAssignment.objects.create(
        user=topology["admin"], station=topology["station_out"], role="maintainer"
    )
    c = bearer(topology["station_user"])
    resp = c.get("/api/v1/station-assignments/")
    assert resp.status_code == 200
    stations = {row["station"] for row in resp.data["results"]}
    assert topology["station_out"].pk not in stations
    assert topology["station_in"].pk in stations


@pytest.mark.django_db
def test_region_assignment_scope_admin_sees_all(topology):  # noqa: F811
    # The topology fixture creates exactly one RegionAssignment (region_mgr → region_in).
    resp = bearer(topology["admin"]).get("/api/v1/region-assignments/")
    assert resp.status_code == 200
    assert resp.data["count"] == 1


@pytest.mark.django_db
def test_region_assignment_scope_member(topology):  # noqa: F811
    # region_mgr manages region_in; must see its assignment, not an out-of-scope one.
    from apps.stations.models import RegionAssignment

    RegionAssignment.objects.create(
        user=topology["admin"], region=topology["region_out"], role="manager"
    )
    resp = bearer(topology["region_mgr"]).get("/api/v1/region-assignments/")
    assert resp.status_code == 200
    regions = {row["region"] for row in resp.data["results"]}
    assert topology["region_in"].pk in regions
    assert topology["region_out"].pk not in regions


@pytest.mark.django_db
def test_station_assignment_scope_region_manager(topology):  # noqa: F811
    # region_mgr manages region_in → sees station assignments on stations in that region.
    from apps.stations.models import StationAssignment

    StationAssignment.objects.create(
        user=topology["admin"], station=topology["station_in"], role="maintainer"
    )
    resp = bearer(topology["region_mgr"]).get("/api/v1/station-assignments/")
    assert resp.status_code == 200
    stations = {row["station"] for row in resp.data["results"]}
    assert topology["station_in"].pk in stations
    assert topology["station_out"].pk not in stations


@pytest.mark.django_db
def test_assignment_applicant_403(topology):  # noqa: F811
    assert bearer(topology["applicant"]).get("/api/v1/station-assignments/").status_code == 403
