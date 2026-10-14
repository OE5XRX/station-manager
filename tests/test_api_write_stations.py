# tests/test_api_write_stations.py
import pytest
from django.urls import reverse

from tests.test_api_read_fixtures import session

pytestmark = pytest.mark.django_db


def _station_payload(region):
    return {"name": "New", "callsign": "OE9ZZZ", "region": region.pk}


def test_region_mgr_cannot_create_station(api_topology, bearer):
    # Region-manager assignments grant no API rights; stations.manage is Staff.
    t = api_topology
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-list"), _station_payload(t["region_in"]), format="json"
    )
    assert r.status_code == 403
    assert r.json()["required"] == "stations.manage"


def test_staff_creates_station_without_region(api_topology, bearer):
    t = api_topology
    r = bearer(t["staff"]).post(
        reverse("api:station-list"), {"name": "New", "callsign": "OE9ZZZ"}, format="json"
    )
    assert r.status_code == 201
    assert r.json()["region"] is None


def test_staff_cannot_create_station_with_region(api_topology, bearer):
    # Setting a region needs topology.manage (Admin) on top of stations.manage.
    t = api_topology
    r = bearer(t["staff"]).post(
        reverse("api:station-list"), _station_payload(t["region_in"]), format="json"
    )
    assert r.status_code == 403
    assert r.json()["required"] == "topology.manage"


def test_staff_create_with_explicit_null_region_allowed(api_topology, bearer):
    t = api_topology
    payload = {"name": "New", "callsign": "OE9ZZZ", "region": None}
    r = bearer(t["staff"]).post(reverse("api:station-list"), payload, format="json")
    assert r.status_code == 201


def test_admin_creates_station_in_region(api_topology, bearer):
    t = api_topology
    r = bearer(t["admin"]).post(
        reverse("api:station-list"), _station_payload(t["region_out"]), format="json"
    )
    assert r.status_code == 201


def test_station_assigned_cannot_update_station(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["station_user"]).patch(url, {"notes": "hi"}, format="json")
    assert r.status_code == 403


def test_region_mgr_cannot_update_or_delete(api_topology, bearer):
    # Region-manager assignments grant no API write rights (stations.manage = Staff).
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    assert bearer(t["region_mgr"]).patch(url, {"notes": "ok"}, format="json").status_code == 403
    assert bearer(t["region_mgr"]).delete(url).status_code == 403


def test_staff_updates_and_deletes_any_station(api_topology, bearer):
    # No topology scoping: staff edits/deletes stations in any region.
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_out"].pk])
    assert bearer(t["staff"]).patch(url, {"notes": "x"}, format="json").status_code == 200
    assert bearer(t["staff"]).delete(url).status_code == 204


def test_unknown_station_update_is_404(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[999999])
    assert bearer(t["staff"]).patch(url, {"notes": "x"}, format="json").status_code == 404


def test_admin_deletes(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    assert bearer(t["admin"]).delete(url).status_code == 204


def test_applicant_and_anon_blocked(api_topology, bearer, anon_client):
    t = api_topology
    url = reverse("api:station-list")
    assert (
        session(t["applicant"])
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


def test_staff_cannot_move_station_to_other_region(api_topology, bearer):
    """Changing `region` needs topology.manage; the row must NOT move."""
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["staff"]).patch(url, {"region": t["region_out"].pk}, format="json")
    assert r.status_code == 403
    assert r.json()["required"] == "topology.manage"
    t["station_in"].refresh_from_db()
    assert t["station_in"].region == t["region_in"]  # unchanged


def test_staff_cannot_clear_station_region(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["staff"]).patch(url, {"region": None}, format="json")
    assert r.status_code == 403
    t["station_in"].refresh_from_db()
    assert t["station_in"].region == t["region_in"]


def test_staff_may_resend_unchanged_region(api_topology, bearer):
    """A PUT/PATCH carrying the station's current region is not a region change."""
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["staff"]).patch(
        url, {"region": t["region_in"].pk, "notes": "same region"}, format="json"
    )
    assert r.status_code == 200


def test_admin_can_move_station_to_any_region(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["admin"]).patch(url, {"region": t["region_out"].pk}, format="json")
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


@pytest.mark.parametrize("who", ["staff", "admin"])
def test_tx_audio_ceiling_not_writable_via_api(api_topology, bearer, who):
    """Calibration is an RF-safety bound set only via the audited station edit view —
    the write API must silently ignore it (value unchanged, no 4xx needed)."""
    t = api_topology
    st = t["station_in"]
    st.tx_audio_ceiling_dbfs = -18.0
    st.save(update_fields=["tx_audio_ceiling_dbfs"])
    url = reverse("api:station-detail", args=[st.pk])
    r = bearer(t[who]).patch(url, {"tx_audio_ceiling_dbfs": -3.0}, format="json")
    assert r.status_code == 200
    st.refresh_from_db()
    assert st.tx_audio_ceiling_dbfs == -18.0
