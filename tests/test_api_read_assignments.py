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
    resp = bearer(topology["admin"]).get("/api/v1/region-assignments/")
    assert resp.status_code == 200
    assert resp.data["count"] >= 1


@pytest.mark.django_db
def test_assignment_applicant_403(topology):  # noqa: F811
    assert bearer(topology["applicant"]).get("/api/v1/station-assignments/").status_code == 403
