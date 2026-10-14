# tests/test_api_write_users.py
"""Tests for the User write endpoint.

Authorization model (unified permissions; email changes go only through the
self-service verification flow):
  - Every write requires ``users.manage`` (Admin) → 403 with
    ``required == "users.manage"`` otherwise, before any lookup/validation.
  - email is read-only (cross-user set = account takeover) — ignored silently.
  - self-change of membership_level → 400 (admins); demote-to-applicant with
    assignments → 400.
  - soft-deleted users are off-limits → 404.
  - Promote/demote direction: MEMBERSHIP_ORDER index (APPLICANT<MEMBER<STAFF<ADMIN).
"""

import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _user_with_level(username, level):
    return User.objects.create_user(username=username, password="x", membership_level=level)


# ---------------------------------------------------------------------------
# Auth / role gates
# ---------------------------------------------------------------------------


def test_anon_gets_401(api_topology, anon_client):
    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    assert anon_client.patch(url, {"bio": "x"}, format="json").status_code == 401


def test_applicant_gets_403(api_topology, bearer):
    t = api_topology
    url = reverse("api:user-detail", args=[t["applicant"].pk])
    assert bearer(t["applicant"]).patch(url, {"bio": "x"}, format="json").status_code == 403


def test_member_cannot_self_elevate(api_topology, bearer):
    """Non-internal user PATCHes own membership → 403; DB unchanged."""
    from apps.accounts.models import User

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["station_user"]).patch(url, {"membership_level": "admin"}, format="json")
    assert r.status_code == 403
    t["station_user"].refresh_from_db()
    assert t["station_user"].membership_level == User.MembershipLevel.MEMBER


def test_member_cannot_patch_other_user(api_topology, bearer):
    """A Member lacks ``users.manage`` → 403 (area gate runs before the lookup)."""
    t = api_topology
    url = reverse("api:user-detail", args=[t["admin"].pk])
    r = bearer(t["station_user"]).patch(url, {"bio": "hi"}, format="json")
    assert r.status_code == 403
    assert r.data["required"] == "users.manage"


# ---------------------------------------------------------------------------
# Write-method restrictions (no POST / no DELETE)
# ---------------------------------------------------------------------------


def test_user_create_405(api_topology, bearer):
    """POST to user-list → 405 (update-only, no create)."""
    t = api_topology
    url = reverse("api:user-list")
    r = bearer(t["admin"]).post(
        url, {"username": "new", "membership_level": "member"}, format="json"
    )
    assert r.status_code == 405


def test_user_delete_405(api_topology, bearer):
    """DELETE on user-detail → 405 (no destroy mixin)."""
    t = api_topology
    r = bearer(t["admin"]).delete(reverse("api:user-detail", args=[t["station_user"].pk]))
    assert r.status_code == 405


# ---------------------------------------------------------------------------
# Privileged flags silently ignored
# ---------------------------------------------------------------------------


def test_privileged_flags_not_writable(api_topology, bearer):
    """is_superuser / is_staff absent from serializer → ignored; stay False."""
    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    bearer(t["admin"]).patch(url, {"is_superuser": True, "is_staff": True}, format="json")
    t["station_user"].refresh_from_db()
    assert t["station_user"].is_superuser is False
    assert t["station_user"].is_staff is False


# ---------------------------------------------------------------------------
# Successful updates (profile fields)
# ---------------------------------------------------------------------------


def test_staff_cannot_update_profile_field(api_topology, bearer):
    """Staff lacks ``users.manage`` → 403; DB unchanged."""
    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["staff"]).patch(url, {"bio": "New bio"}, format="json")
    assert r.status_code == 403
    assert r.data["required"] == "users.manage"
    t["station_user"].refresh_from_db()
    assert t["station_user"].bio == ""


def test_admin_updates_profile_field(api_topology, bearer):
    """Admin can update profile fields on another user."""
    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"qth_name": "Vienna"}, format="json")
    assert r.status_code == 200
    t["station_user"].refresh_from_db()
    assert t["station_user"].qth_name == "Vienna"


# ---------------------------------------------------------------------------
# Membership changes + audit
# ---------------------------------------------------------------------------


def test_admin_sets_membership_and_audits(api_topology, bearer):
    """Admin PATCHes another user's membership_level → 200; DB updated;
    MEMBERSHIP_PROMOTED AccountAuditLog row with target_user + 'via API token'.

    Membership changes require ``users.manage`` (Admin).
    """
    from apps.accounts.models import AccountAuditLog, User

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"membership_level": "staff"}, format="json")
    assert r.status_code == 200
    t["station_user"].refresh_from_db()
    assert t["station_user"].membership_level == User.MembershipLevel.STAFF
    log = AccountAuditLog.objects.filter(
        target_user=t["station_user"],
        event_type=AccountAuditLog.EventType.MEMBERSHIP_PROMOTED,
    )
    assert log.exists()
    assert "via API token" in log.first().message


def test_staff_cannot_change_others_membership(api_topology, bearer):
    """A STAFF actor lacks ``users.manage`` → 403; DB unchanged."""
    from apps.accounts.models import User

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["staff"]).patch(url, {"membership_level": "staff"}, format="json")
    assert r.status_code == 403
    t["station_user"].refresh_from_db()
    assert t["station_user"].membership_level == User.MembershipLevel.MEMBER


def test_demotion_audits_membership_demoted(api_topology, bearer):
    """Demoting a user's membership_level creates a MEMBERSHIP_DEMOTED audit row."""
    from apps.accounts.models import AccountAuditLog, User

    t = api_topology
    # Promote first
    t["station_user"].membership_level = User.MembershipLevel.STAFF
    t["station_user"].save(update_fields=["membership_level"])

    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"membership_level": "member"}, format="json")
    assert r.status_code == 200
    t["station_user"].refresh_from_db()
    assert t["station_user"].membership_level == User.MembershipLevel.MEMBER
    assert AccountAuditLog.objects.filter(
        target_user=t["station_user"],
        event_type=AccountAuditLog.EventType.MEMBERSHIP_DEMOTED,
    ).exists()


def test_unchanged_membership_audits_user_updated(api_topology, bearer):
    """Updating a non-membership field creates USER_UPDATED (not promoted/demoted)."""
    from apps.accounts.models import AccountAuditLog

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"bio": "just a bio"}, format="json")
    assert r.status_code == 200
    assert AccountAuditLog.objects.filter(
        target_user=t["station_user"],
        event_type=AccountAuditLog.EventType.USER_UPDATED,
    ).exists()


# ---------------------------------------------------------------------------
# UI-mirrored self-change guard
# ---------------------------------------------------------------------------


def test_staff_cannot_change_own_membership(api_topology, bearer):
    """Staff cannot change their own membership_level → 403.

    The ``users.manage`` area gate fires before the self-change guard (which
    still applies to admins — see test_admin_cannot_change_own_membership).
    """
    t = api_topology
    url = reverse("api:user-detail", args=[t["staff"].pk])
    r = bearer(t["staff"]).patch(url, {"membership_level": "admin"}, format="json")
    assert r.status_code == 403


def test_admin_cannot_change_own_membership(api_topology, bearer):
    """Admin cannot change their own membership_level → 400.
    Mirrors MembershipSetView self-change guard.
    """
    t = api_topology
    url = reverse("api:user-detail", args=[t["admin"].pk])
    r = bearer(t["admin"]).patch(url, {"membership_level": "member"}, format="json")
    assert r.status_code == 400


def test_staff_cannot_edit_own_profile_via_admin_api(api_topology, bearer):
    """This is the ADMIN-management endpoint (``users.manage``), not the
    self-service profile flow → 403 for staff, even on themselves. Self
    profile editing lives in the dedicated ProfileForm UI flow, not here.
    """
    t = api_topology
    url = reverse("api:user-detail", args=[t["staff"].pk])
    r = bearer(t["staff"]).patch(url, {"bio": "my own bio"}, format="json")
    assert r.status_code == 403


def test_admin_can_edit_own_profile_fields(api_topology, bearer):
    """An admin (``users.manage``) may edit their own non-membership profile
    fields. The self-change guard only blocks membership_level changes.
    """
    t = api_topology
    url = reverse("api:user-detail", args=[t["admin"].pk])
    r = bearer(t["admin"]).patch(url, {"bio": "admin own bio"}, format="json")
    assert r.status_code == 200
    t["admin"].refresh_from_db()
    assert t["admin"].bio == "admin own bio"


# ---------------------------------------------------------------------------
# UI-mirrored demote-to-applicant guard
# ---------------------------------------------------------------------------


def test_demote_to_applicant_blocked_when_assignments_exist(api_topology, bearer):
    """Cannot demote to applicant when the user has assignments.
    Mirrors MembershipSetView's assignment-check guard → 400.
    """
    t = api_topology
    # station_user already has a StationAssignment in api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"membership_level": "applicant"}, format="json")
    assert r.status_code == 400


def test_demote_to_applicant_allowed_when_no_assignments(api_topology, bearer):
    """Demote to applicant succeeds when the user has no assignments."""
    from apps.accounts.models import User

    t = api_topology
    # region_mgr has a RegionAssignment — use admin who has none
    # Create a fresh user with no assignments
    fresh = _user_with_level("fresh_member", User.MembershipLevel.MEMBER)

    url = reverse("api:user-detail", args=[fresh.pk])
    r = bearer(t["admin"]).patch(url, {"membership_level": "applicant"}, format="json")
    assert r.status_code == 200
    fresh.refresh_from_db()
    assert fresh.membership_level == User.MembershipLevel.APPLICANT


# ---------------------------------------------------------------------------
# Membership change takes effect on the permission registry
# ---------------------------------------------------------------------------


def test_membership_change_grants_new_level_rights(api_topology, bearer):
    """After promoting to staff the target gets the Staff rights of the
    registry (``can`` reads the level straight from the row — no role cache).
    """
    from apps.accounts.permissions import can

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"membership_level": "staff"}, format="json")
    assert r.status_code == 200
    target = User.objects.get(pk=t["station_user"].pk)
    assert can(target, "stations", "manage") is True
    assert can(target, "users", "manage") is False


# ---------------------------------------------------------------------------
# CRITICAL 1 — email is read-only (account-takeover prevention)
# ---------------------------------------------------------------------------


def test_email_not_writable(api_topology, bearer):
    """Setting another user's email is an account-takeover vector; email is
    read-only → the attempted value is ignored, DB email unchanged.
    """
    t = api_topology
    original = t["station_user"].email
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"email": "x@evil.com"}, format="json")
    assert r.status_code == 200
    t["station_user"].refresh_from_db()
    assert t["station_user"].email == original
    assert t["station_user"].email != "x@evil.com"


# ---------------------------------------------------------------------------
# IMPORTANT 3 — soft-deleted users off-limits
# ---------------------------------------------------------------------------


def test_patch_soft_deleted_user_404(api_topology, bearer):
    """A soft-deleted user is outside the directory queryset → PATCH → 404."""
    from django.utils import timezone

    t = api_topology
    t["station_user"].deleted_at = timezone.now()
    t["station_user"].save(update_fields=["deleted_at"])

    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"bio": "ghost"}, format="json")
    assert r.status_code == 404


def test_admin_can_patch_deactivated_user(api_topology, bearer):
    """Deactivated (not deleted) users stay in the admin's directory → 200."""
    t = api_topology
    t["station_user"].is_active = False
    t["station_user"].save(update_fields=["is_active"])
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["admin"]).patch(url, {"bio": "dormant"}, format="json")
    assert r.status_code == 200
    t["station_user"].refresh_from_db()
    assert t["station_user"].bio == "dormant"


# ---------------------------------------------------------------------------
# Target level does not matter — only the actor's users.manage
# ---------------------------------------------------------------------------


def test_staff_cannot_edit_another_staff(api_topology, bearer):
    """A staff actor lacks ``users.manage`` → 403 on any target."""
    t = api_topology
    peer = _user_with_level("api_staff2", User.MembershipLevel.STAFF)
    url = reverse("api:user-detail", args=[peer.pk])
    r = bearer(t["staff"]).patch(url, {"bio": "nope"}, format="json")
    assert r.status_code == 403
    peer.refresh_from_db()
    assert peer.bio == ""


def test_staff_cannot_edit_a_member(api_topology, bearer):
    """Old rule let staff edit non-internal targets; now ``users.manage`` → 403."""
    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["staff"]).patch(url, {"bio": "ok member"}, format="json")
    assert r.status_code == 403
    t["station_user"].refresh_from_db()
    assert t["station_user"].bio == ""


def test_admin_can_edit_a_staff(api_topology, bearer):
    """An admin (``users.manage``) MAY edit a staff target's profile → 200."""
    t = api_topology
    url = reverse("api:user-detail", args=[t["staff"].pk])
    r = bearer(t["admin"]).patch(url, {"bio": "admin edits staff"}, format="json")
    assert r.status_code == 200
    t["staff"].refresh_from_db()
    assert t["staff"].bio == "admin edits staff"
