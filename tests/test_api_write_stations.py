# tests/test_api_write_stations.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def _station_payload(region):
    return {"name": "New", "callsign": "OE9ZZZ", "region": region.pk}


def test_region_mgr_creates_in_own_region(api_topology, bearer):
    t = api_topology
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-list"), _station_payload(t["region_in"]), format="json"
    )
    assert r.status_code == 201


def test_region_mgr_cannot_create_in_other_region(api_topology, bearer):
    t = api_topology
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-list"), _station_payload(t["region_out"]), format="json"
    )
    assert r.status_code == 403


def test_station_assigned_cannot_update_station(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["station_user"]).patch(url, {"notes": "hi"}, format="json")
    assert r.status_code == 403


def test_region_mgr_updates_in_scope_but_cannot_delete(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    assert bearer(t["region_mgr"]).patch(url, {"notes": "ok"}, format="json").status_code == 200
    assert bearer(t["region_mgr"]).delete(url).status_code == 403  # delete = staff/admin


def test_out_of_scope_update_is_404(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_out"].pk])
    assert bearer(t["region_mgr"]).patch(url, {"notes": "x"}, format="json").status_code == 404


def test_admin_deletes(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    assert bearer(t["admin"]).delete(url).status_code == 204


def test_applicant_and_anon_blocked(api_topology, bearer, anon_client):
    t = api_topology
    url = reverse("api:station-list")
    assert (
        bearer(t["applicant"])
        .post(url, _station_payload(t["region_in"]), format="json")
        .status_code
        == 403
    )
    assert (
        anon_client.post(url, _station_payload(t["region_in"]), format="json").status_code == 401
    )


def test_status_field_not_writable(api_topology, bearer):
    """Review Focus: curated writable fields — status is server-controlled."""
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    before = t["station_in"].status
    bearer(t["staff"]).patch(url, {"status": "online"}, format="json")
    t["station_in"].refresh_from_db()
    assert t["station_in"].status == before  # status is read-only, PATCH ignores it


def test_region_mgr_cannot_move_station_to_out_of_scope_region(api_topology, bearer):
    """C2: `region` is writable; perform_update must re-validate the NEW region.

    A region_mgr of region_in manages station_in. Attempting to PATCH its
    `region` to region_out (not managed) must 403, and the row must NOT move.
    """
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["region_mgr"]).patch(url, {"region": t["region_out"].pk}, format="json")
    assert r.status_code == 403
    t["station_in"].refresh_from_db()
    assert t["station_in"].region == t["region_in"]  # unchanged


def test_staff_can_move_station_to_any_region(api_topology, bearer):
    """Guard isn't over-restrictive: staff (is_internal) may move a station."""
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["staff"]).patch(url, {"region": t["region_out"].pk}, format="json")
    assert r.status_code == 200
    t["station_in"].refresh_from_db()
    assert t["station_in"].region == t["region_out"]


def test_write_creates_audit_entry(api_topology, bearer):
    from apps.stations.models import StationAuditLog

    t = api_topology
    bearer(t["staff"]).patch(
        reverse("api:station-detail", args=[t["station_in"].pk]),
        {"notes": "audited"},
        format="json",
    )
    log = StationAuditLog.objects.filter(
        station=t["station_in"], event_type=StationAuditLog.EventType.UPDATED
    ).latest("created_at")
    assert "via API token" in log.message
