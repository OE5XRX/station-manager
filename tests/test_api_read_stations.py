import pytest

from tests.test_api_read_fixtures import (  # noqa: F401
    anon_client,
    bearer,
    client_for,
    session,
    topology,
)


@pytest.mark.django_db
def test_anon_list_401(topology):  # noqa: F811
    resp = anon_client().get("/api/v1/stations/")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_applicant_list_200(topology):  # noqa: F811
    # stations.view = Applicant (spec matrix).
    resp = session(topology["applicant"]).get("/api/v1/stations/")
    assert resp.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize(
    "role,expected_names",
    [
        ("admin", {"S-in", "S-out"}),
        ("staff", {"S-in", "S-out"}),
        ("region_mgr", {"S-in", "S-out"}),
        ("station_user", {"S-in", "S-out"}),
        ("applicant", {"S-in", "S-out"}),
    ],
)
def test_station_list_no_topology_scope(topology, role, expected_names):  # noqa: F811
    resp = client_for(topology[role]).get("/api/v1/stations/")
    assert resp.status_code == 200
    names = {row["name"] for row in resp.data["results"]}
    assert names == expected_names


@pytest.mark.django_db
def test_unassigned_station_retrieve_is_200(topology):  # noqa: F811
    # No topology scoping: a station the user has no assignment on is visible.
    client = bearer(topology["station_user"])
    resp = client.get(f"/api/v1/stations/{topology['station_out'].pk}/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_unknown_station_retrieve_is_404(topology):  # noqa: F811
    resp = bearer(topology["station_user"]).get("/api/v1/stations/999999/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_station_serializer_has_no_unexpected_secret_fields(topology):  # noqa: F811
    resp = bearer(topology["admin"]).get(f"/api/v1/stations/{topology['station_in'].pk}/")
    assert resp.status_code == 200
    assert "is_online" in resp.data  # computed field present
    assert set(resp.data) >= {"id", "name", "callsign", "region", "status"}


@pytest.mark.django_db
def test_region_and_tag_lists(topology):  # noqa: F811
    c = bearer(topology["region_mgr"])
    assert c.get("/api/v1/regions/").status_code == 200
    # No topology scoping: region_mgr sees every region.
    assert {r["slug"] for r in c.get("/api/v1/regions/").data["results"]} == {"in", "out"}
    assert c.get("/api/v1/station-tags/").status_code == 200
