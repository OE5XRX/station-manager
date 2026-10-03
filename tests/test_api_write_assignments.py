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


# ---------------------------------------------------------------------------
# Batch B regression tests
# ---------------------------------------------------------------------------


def test_duplicate_station_assignment_returns_400(api_topology, bearer):
    """B2: creating a second StationAssignment for the same (user, station)
    returns 400 (uniqueness validation), not 500.
    """

    t = api_topology
    # station_user already has an assignment on station_in (from api_topology fixture)
    payload = {
        "user": t["station_user"].pk,
        "station": t["station_in"].pk,
        "role": "admin",
    }
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-assignment-list"), payload, format="json"
    )
    assert r.status_code in (400, 409), f"Expected 400 or 409, got {r.status_code}: {r.data}"


def test_second_admin_assignment_same_station_returns_400(api_topology, bearer):
    """B2: creating a second admin-role assignment for a station that already
    has an admin returns 400/409, not 500.
    """
    from apps.stations.models import StationAssignment

    t = api_topology
    # Create first admin assignment
    StationAssignment.objects.create(user=t["staff"], station=t["station_in"], role="admin")
    # Now try to create a second admin for the same station
    new_member = __import__("apps.accounts.models", fromlist=["User"]).User.objects.create_user(
        "extra_member", password="x"
    )
    new_member.membership_level = "member"
    new_member.save()
    payload = {
        "user": new_member.pk,
        "station": t["station_in"].pk,
        "role": "admin",
    }
    r = bearer(t["admin"]).post(reverse("api:station-assignment-list"), payload, format="json")
    assert r.status_code in (400, 409), f"Expected 400 or 409, got {r.status_code}: {r.data}"


def test_assigned_by_unchanged_on_patch(api_topology, bearer):
    """B3: PATCHing a StationAssignment's role must NOT rewrite assigned_by.
    assigned_by should remain the original creator, not the editor.
    """
    from apps.stations.models import StationAssignment

    t = api_topology
    # Create an assignment as region_mgr
    payload = {"user": t["staff"].pk, "station": t["station_in"].pk, "role": "maintainer"}
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-assignment-list"), payload, format="json"
    )
    assert r.status_code == 201, r.data
    assignment_pk = r.data["id"]
    assignment = StationAssignment.objects.get(pk=assignment_pk)
    assert assignment.assigned_by == t["region_mgr"]

    # Now staff (admin) PATCHes the role
    detail = reverse("api:station-assignment-detail", args=[assignment_pk])
    r2 = bearer(t["admin"]).patch(detail, {"role": "admin"}, format="json")
    assert r2.status_code == 200, r2.data
    assignment.refresh_from_db()
    assert assignment.role == "admin"
    # assigned_by must NOT have changed to admin
    assert assignment.assigned_by == t["region_mgr"], (
        f"assigned_by was rewritten to {assignment.assigned_by}, expected region_mgr"
    )


def test_soft_deleted_user_rejected_for_station_assignment(api_topology, bearer):
    """B4: POSTing a StationAssignment for a soft-deleted user → 400, no row."""
    from django.utils import timezone

    from apps.accounts.models import User
    from apps.stations.models import StationAssignment

    t = api_topology
    victim = User.objects.create_user("soft_deleted_sa", password="x")
    victim.membership_level = "member"
    victim.deleted_at = timezone.now()
    victim.save()

    payload = {
        "user": victim.pk,
        "station": t["station_in"].pk,
        "role": "maintainer",
    }
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-assignment-list"), payload, format="json"
    )
    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.data}"
    assert not StationAssignment.objects.filter(user=victim).exists()


def test_soft_deleted_user_rejected_for_region_assignment(api_topology, bearer):
    """B4: POSTing a RegionAssignment for a soft-deleted user → 400, no row."""
    from django.utils import timezone

    from apps.accounts.models import User
    from apps.stations.models import RegionAssignment

    t = api_topology
    victim = User.objects.create_user("soft_deleted_ra", password="x")
    victim.membership_level = "member"
    victim.deleted_at = timezone.now()
    victim.save()

    payload = {
        "user": victim.pk,
        "region": t["region_in"].pk,
        "role": "manager",
    }
    r = bearer(t["admin"]).post(reverse("api:region-assignment-list"), payload, format="json")
    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.data}"
    assert not RegionAssignment.objects.filter(user=victim).exists()
