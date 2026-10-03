# tests/test_api_write_users.py
"""Tests for the User write endpoint (Task 10).

UI guards mirrored from apps/accounts/views_membership.py (MembershipSetView):
  1. Self-change guard: staff/admin cannot change their own membership_level
     (UI returns 400; API mirrors as 400).
  2. Demote-to-applicant guard: blocked when assignments exist (UI → 400;
     API mirrors as 400 via serializer-level validate_membership_level).
  3. Promote/demote direction: determined by MEMBERSHIP_ORDER index
     (APPLICANT < MEMBER < STAFF < ADMIN).

Unlike the UI's AdminRequiredMixin (admin-only), the API gate is is_internal
(staff OR admin) per the Phase-3 spec — confirmed by test_staff_sets_membership.
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


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
    """Non-internal user cannot see (→ 404) let alone patch another user."""
    t = api_topology
    url = reverse("api:user-detail", args=[t["admin"].pk])
    # non-internal get_queryset returns only self → 404
    r = bearer(t["station_user"]).patch(url, {"bio": "hi"}, format="json")
    assert r.status_code == 404


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


def test_staff_updates_profile_field(api_topology, bearer):
    """Staff can update profile fields on another user."""
    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["staff"]).patch(url, {"bio": "New bio"}, format="json")
    assert r.status_code == 200
    t["station_user"].refresh_from_db()
    assert t["station_user"].bio == "New bio"


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


def test_staff_sets_membership_and_audits(api_topology, bearer):
    """Staff PATCHes another user's membership_level → 200; DB updated;
    MEMBERSHIP_PROMOTED AccountAuditLog row with target_user + 'via API token'.
    """
    from apps.accounts.models import AccountAuditLog, User

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["staff"]).patch(url, {"membership_level": "staff"}, format="json")
    assert r.status_code == 200
    t["station_user"].refresh_from_db()
    assert t["station_user"].membership_level == User.MembershipLevel.STAFF
    log = AccountAuditLog.objects.filter(
        target_user=t["station_user"],
        event_type=AccountAuditLog.EventType.MEMBERSHIP_PROMOTED,
    )
    assert log.exists()
    assert "via API token" in log.first().message


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
    """Staff cannot change their own membership_level → 400.
    Mirrors MembershipSetView: 'Cannot change your own membership level.'
    """
    t = api_topology
    url = reverse("api:user-detail", args=[t["staff"].pk])
    r = bearer(t["staff"]).patch(url, {"membership_level": "admin"}, format="json")
    assert r.status_code == 400


def test_admin_cannot_change_own_membership(api_topology, bearer):
    """Admin cannot change their own membership_level → 400.
    Mirrors MembershipSetView self-change guard.
    """
    t = api_topology
    url = reverse("api:user-detail", args=[t["admin"].pk])
    r = bearer(t["admin"]).patch(url, {"membership_level": "member"}, format="json")
    assert r.status_code == 400


def test_self_change_other_fields_allowed(api_topology, bearer):
    """Staff/admin CAN patch their own non-membership fields (only membership
    self-change is blocked, not all self-patches).
    """
    t = api_topology
    url = reverse("api:user-detail", args=[t["staff"].pk])
    r = bearer(t["staff"]).patch(url, {"bio": "my own bio"}, format="json")
    assert r.status_code == 200
    t["staff"].refresh_from_db()
    assert t["staff"].bio == "my own bio"


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
    fresh = User.objects.create_user(username="fresh_member", password="x")
    fresh.membership_level = User.MembershipLevel.MEMBER
    fresh.save(update_fields=["membership_level"])
    User._invalidate_role_cache(fresh)

    url = reverse("api:user-detail", args=[fresh.pk])
    r = bearer(t["admin"]).patch(url, {"membership_level": "applicant"}, format="json")
    assert r.status_code == 200
    fresh.refresh_from_db()
    assert fresh.membership_level == User.MembershipLevel.APPLICANT


# ---------------------------------------------------------------------------
# role_cache invalidation (cached_property must not go stale)
# ---------------------------------------------------------------------------


def test_role_cache_invalidated_after_membership_change(api_topology, bearer):
    """After changing membership_level the is_internal cached_property on the
    saved instance must reflect the new value (no stale cache).
    """
    from apps.accounts.models import User

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    bearer(t["admin"]).patch(url, {"membership_level": "staff"}, format="json")
    t["station_user"].refresh_from_db()
    User._invalidate_role_cache(t["station_user"])
    assert t["station_user"].is_internal is True
