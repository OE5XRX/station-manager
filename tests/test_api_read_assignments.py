import pytest

from tests.test_api_read_fixtures import bearer, session, topology  # noqa: F401


@pytest.mark.django_db
def test_station_assignment_no_topology_scope(topology):  # noqa: F811
    # topology.view is Applicant: every user sees every assignment (like the UI).
    from apps.stations.models import StationAssignment

    StationAssignment.objects.create(
        user=topology["admin"], station=topology["station_out"], role="maintainer"
    )
    c = bearer(topology["station_user"])
    resp = c.get("/api/v1/station-assignments/")
    assert resp.status_code == 200
    stations = {row["station"] for row in resp.data["results"]}
    assert topology["station_out"].pk in stations
    assert topology["station_in"].pk in stations


@pytest.mark.django_db
def test_region_assignment_scope_admin_sees_all(topology):  # noqa: F811
    # The topology fixture creates exactly one RegionAssignment (region_mgr → region_in).
    resp = bearer(topology["admin"]).get("/api/v1/region-assignments/")
    assert resp.status_code == 200
    assert resp.data["count"] == 1


@pytest.mark.django_db
def test_region_assignment_no_topology_scope(topology):  # noqa: F811
    # No topology scoping: region_mgr sees assignments outside its own region too.
    from apps.stations.models import RegionAssignment

    RegionAssignment.objects.create(
        user=topology["admin"], region=topology["region_out"], role="manager"
    )
    resp = bearer(topology["region_mgr"]).get("/api/v1/region-assignments/")
    assert resp.status_code == 200
    regions = {row["region"] for row in resp.data["results"]}
    assert topology["region_in"].pk in regions
    assert topology["region_out"].pk in regions


@pytest.mark.django_db
def test_station_assignment_scope_region_manager(topology):  # noqa: F811
    # region_mgr sees station assignments on every station, not just its region.
    from apps.stations.models import StationAssignment

    StationAssignment.objects.create(
        user=topology["admin"], station=topology["station_in"], role="maintainer"
    )
    resp = bearer(topology["region_mgr"]).get("/api/v1/station-assignments/")
    assert resp.status_code == 200
    stations = {row["station"] for row in resp.data["results"]}
    assert topology["station_in"].pk in stations


@pytest.mark.django_db
def test_assignment_applicant_can_read(topology):  # noqa: F811
    # topology.view = Applicant (spec matrix).
    assert session(topology["applicant"]).get("/api/v1/station-assignments/").status_code == 200


@pytest.fixture
def hidden_assignments(topology):  # noqa: F811
    """Assignments of a directory-hidden user, an inactive one and an applicant."""
    from apps.accounts.models import User
    from apps.stations.models import RegionAssignment, StationAssignment

    hidden = User.objects.create_user(
        username="hidden", password="x", membership_level="member", is_directory_visible=False
    )
    inactive = User.objects.create_user(
        username="gone", password="x", membership_level="member", is_active=False
    )
    demoted = User.objects.create_user(username="demoted", password="x", membership_level="member")
    for u in (hidden, inactive, demoted):
        StationAssignment.objects.create(user=u, station=topology["station_in"], role="maintainer")
        RegionAssignment.objects.create(user=u, region=topology["region_in"], role="manager")
    # A member who held assignments and was later demoted to Applicant. Use a
    # queryset .update() so no save()-side cleanup can drop the assignments.
    User.objects.filter(pk=demoted.pk).update(membership_level="applicant")
    demoted.refresh_from_db()
    topology["hidden"] = hidden
    topology["inactive"] = inactive
    topology["demoted"] = demoted
    return topology


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["station-assignments", "region-assignments"])
def test_assignments_of_hidden_users_not_exposed_to_members(hidden_assignments, url):
    t = hidden_assignments
    users = {r["user"] for r in bearer(t["station_user"]).get(f"/api/v1/{url}/").data["results"]}
    assert t["hidden"].pk not in users and t["inactive"].pk not in users
    assert t["demoted"].pk not in users
    # Filtering by the hidden user's id reveals nothing either.
    q = bearer(t["station_user"]).get(f"/api/v1/{url}/?user={t['hidden'].pk}")
    assert q.data["count"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["station-assignments", "region-assignments"])
def test_assignments_of_hidden_users_visible_to_admin(hidden_assignments, url):
    t = hidden_assignments
    users = {r["user"] for r in bearer(t["admin"]).get(f"/api/v1/{url}/").data["results"]}
    assert t["hidden"].pk in users and t["inactive"].pk in users
    assert t["demoted"].pk in users


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["station-assignments", "region-assignments"])
def test_hidden_user_sees_own_assignments(hidden_assignments, url):
    t = hidden_assignments
    users = {r["user"] for r in bearer(t["hidden"]).get(f"/api/v1/{url}/").data["results"]}
    assert t["hidden"].pk in users


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["station-assignments", "region-assignments"])
def test_applicant_viewer_sees_only_own_assignments(hidden_assignments, url):
    # Mirrors audience_for: an Applicant has no view of any other user, so the
    # API must not reveal other members' assignments either — only its own.
    # The demoted user is an Applicant that still holds assignments (applicants
    # cannot be given new ones), so it exercises the "own rows" branch.
    t = hidden_assignments
    resp = session(t["demoted"]).get(f"/api/v1/{url}/")
    assert resp.status_code == 200
    users = {r["user"] for r in resp.data["results"]}
    assert users == {t["demoted"].pk}
