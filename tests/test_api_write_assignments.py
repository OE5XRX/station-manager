"""Write-surface permission matrix tests for StationAssignment + RegionAssignment.

TDD: run RED first (router still points to read viewsets), then implement and
verify GREEN.
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_region_mgr_assigns_in_scope(api_topology, bearer):
    t = api_topology
    payload = {"user": t["applicant"].pk, "station": t["station_in"].pk, "role": "maintainer"}
    # applicant target is rejected by _ApplicantForbiddenMixin -> 400
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-assignment-list"), payload, format="json"
    )
    assert r.status_code == 400
    payload["user"] = t["staff"].pk
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-assignment-list"), payload, format="json"
    )
    assert r.status_code == 201


def test_region_mgr_cannot_assign_out_of_scope_station(api_topology, bearer):
    t = api_topology
    payload = {"user": t["staff"].pk, "station": t["station_out"].pk, "role": "maintainer"}
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-assignment-list"), payload, format="json"
    )
    assert r.status_code == 403


def test_assigned_by_is_server_side(api_topology, bearer):
    from apps.stations.models import StationAssignment

    t = api_topology
    payload = {
        "user": t["staff"].pk,
        "station": t["station_in"].pk,
        "role": "maintainer",
        "assigned_by": t["admin"].pk,  # attacker-supplied, must be ignored
    }
    bearer(t["region_mgr"]).post(reverse("api:station-assignment-list"), payload, format="json")
    a = StationAssignment.objects.get(user=t["staff"], station=t["station_in"])
    assert a.assigned_by == t["region_mgr"]


def test_region_assignment_staff_only(api_topology, bearer):
    t = api_topology
    payload = {"user": t["staff"].pk, "region": t["region_in"].pk, "role": "manager"}
    assert (
        bearer(t["region_mgr"])
        .post(reverse("api:region-assignment-list"), payload, format="json")
        .status_code
        == 403
    )
    assert (
        bearer(t["admin"])
        .post(reverse("api:region-assignment-list"), payload, format="json")
        .status_code
        == 201
    )


def test_station_assignment_create_audit_row(api_topology, bearer):
    """API create emits an AccountAuditLog row with 'via API token' in message."""
    from apps.accounts.models import AccountAuditLog

    t = api_topology
    payload = {"user": t["staff"].pk, "station": t["station_in"].pk, "role": "maintainer"}
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-assignment-list"), payload, format="json"
    )
    assert r.status_code == 201
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        message__contains="via API token",
    ).exists()


def test_station_assignment_delete(api_topology, bearer):
    """region_mgr can delete an in-scope StationAssignment."""
    from apps.stations.models import StationAssignment

    t = api_topology
    assignment = StationAssignment.objects.create(
        user=t["staff"], station=t["station_in"], role="maintainer"
    )
    url = reverse("api:station-assignment-detail", args=[assignment.pk])
    r = bearer(t["region_mgr"]).delete(url)
    assert r.status_code == 204
    assert not StationAssignment.objects.filter(pk=assignment.pk).exists()


def test_station_assignment_delete_out_of_scope(api_topology, bearer):
    """region_mgr cannot delete an assignment on station_out.

    The assignment is out of the region_mgr's read scope, so get_object()
    returns 404 (invisible object), not 403. This matches the Phase-2
    read-scope contract: inaccessible == not found.
    """
    from apps.stations.models import StationAssignment

    t = api_topology
    assignment = StationAssignment.objects.create(
        user=t["staff"], station=t["station_out"], role="maintainer"
    )
    url = reverse("api:station-assignment-detail", args=[assignment.pk])
    r = bearer(t["region_mgr"]).delete(url)
    # 404: the assignment is not in region_mgr's read queryset
    assert r.status_code == 404
    # Confirm it was NOT deleted
    assert StationAssignment.objects.filter(pk=assignment.pk).exists()


def test_region_assignment_delete_staff_only(api_topology, bearer):
    """Only staff/admin may delete a RegionAssignment; region_mgr gets 403."""
    from apps.stations.models import RegionAssignment

    t = api_topology
    assignment = RegionAssignment.objects.create(
        user=t["staff"], region=t["region_in"], role="manager"
    )
    url = reverse("api:region-assignment-detail", args=[assignment.pk])
    assert bearer(t["region_mgr"]).delete(url).status_code == 403
    assert bearer(t["admin"]).delete(url).status_code == 204


def test_station_assignment_update(api_topology, bearer):
    """region_mgr can update (PATCH) an in-scope StationAssignment."""
    from apps.stations.models import StationAssignment

    t = api_topology
    assignment = StationAssignment.objects.create(
        user=t["staff"], station=t["station_in"], role="maintainer"
    )
    url = reverse("api:station-assignment-detail", args=[assignment.pk])
    r = bearer(t["region_mgr"]).patch(url, {"role": "admin"}, format="json")
    assert r.status_code == 200
    assignment.refresh_from_db()
    assert assignment.role == "admin"


def test_region_mgr_cannot_move_assignment_to_out_of_scope_station(api_topology, bearer):
    """C1: `station` is writable; perform_update must re-validate the NEW station.

    region_mgr of region_in may edit an in-scope assignment, but must NOT be
    able to PATCH its `station` to station_out (out of scope) -> 403, DB
    unchanged.
    """
    from apps.stations.models import StationAssignment

    t = api_topology
    assignment = StationAssignment.objects.create(
        user=t["staff"], station=t["station_in"], role="maintainer"
    )
    url = reverse("api:station-assignment-detail", args=[assignment.pk])
    r = bearer(t["region_mgr"]).patch(url, {"station": t["station_out"].pk}, format="json")
    assert r.status_code == 403
    assignment.refresh_from_db()
    assert assignment.station == t["station_in"]  # unchanged


def test_staff_can_move_assignment_to_any_station(api_topology, bearer):
    """Guard isn't over-restrictive: staff (is_internal) may move an assignment."""
    from apps.stations.models import StationAssignment

    t = api_topology
    assignment = StationAssignment.objects.create(
        user=t["staff"], station=t["station_in"], role="maintainer"
    )
    url = reverse("api:station-assignment-detail", args=[assignment.pk])
    r = bearer(t["admin"]).patch(url, {"station": t["station_out"].pk}, format="json")
    assert r.status_code == 200
    assignment.refresh_from_db()
    assert assignment.station == t["station_out"]


def test_station_assignment_update_audit_uses_updated_event(api_topology, bearer):
    """Update emits STATION_ASSIGNMENT_UPDATED (not _CREATED) with token origin."""
    from apps.accounts.models import AccountAuditLog
    from apps.stations.models import StationAssignment

    t = api_topology
    assignment = StationAssignment.objects.create(
        user=t["staff"], station=t["station_in"], role="maintainer"
    )
    url = reverse("api:station-assignment-detail", args=[assignment.pk])
    bearer(t["region_mgr"]).patch(url, {"role": "admin"}, format="json")
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_UPDATED,
        message__contains="via API token",
    ).exists()


def test_anon_station_assignment_create_401(api_topology, anon_client):
    """Unauthenticated POST returns 401."""
    t = api_topology
    payload = {"user": t["staff"].pk, "station": t["station_in"].pk, "role": "maintainer"}
    r = anon_client.post(reverse("api:station-assignment-list"), payload, format="json")
    assert r.status_code == 401
