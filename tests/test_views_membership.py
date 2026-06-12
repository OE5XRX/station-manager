"""Tests for MembershipSetView (promote/demote endpoint)."""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import (
    Station,
    StationAssignment,
)


def _user(level, username):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestMembershipSetView:
    def test_admin_can_promote_applicant_to_member(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.APPLICANT, "hans")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "member"},
        )
        assert response.status_code == 200
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.MEMBER

    def test_promotion_emits_membership_promoted_audit_log(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.APPLICANT, "hans")
        client.force_login(admin)
        client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "staff"},
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.MEMBERSHIP_PROMOTED,
            actor=admin,
            target_user=target,
        ).first()
        assert entry is not None
        assert "applicant" in entry.message.lower()
        assert "staff" in entry.message.lower()

    def test_demotion_emits_membership_demoted_audit_log(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.STAFF, "maria")
        client.force_login(admin)
        client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "member"},
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.MEMBERSHIP_DEMOTED,
            actor=admin,
            target_user=target,
        ).first()
        assert entry is not None
        assert "staff" in entry.message.lower()
        assert "member" in entry.message.lower()

    def test_no_change_does_not_emit_audit_log(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        client.force_login(admin)
        client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "member"},
        )
        assert AccountAuditLog.objects.filter(target_user=target).count() == 0

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        client.force_login(staff)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "admin"},
        )
        assert response.status_code in (302, 403)
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.MEMBER

    def test_self_forbidden(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[admin.pk]),
            {"level": "member"},
        )
        assert response.status_code == 400
        admin.refresh_from_db()
        assert admin.membership_level == User.MembershipLevel.ADMIN

    def test_demote_to_applicant_blocked_when_assignments_exist(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(user=target, station=s, role=StationAssignment.Role.ADMIN)
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "applicant"},
        )
        assert response.status_code == 400
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.MEMBER

    def test_demote_to_applicant_clean_user_ok(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "applicant"},
        )
        assert response.status_code == 200
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.APPLICANT

    def test_invalid_level_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "godlike"},
        )
        assert response.status_code == 400


@pytest.mark.django_db
def test_user_detail_renders_membership_card_for_admin(client, admin_user):
    """Admin viewing another user sees the membership picker on the detail page.

    Cards moved from user_form.html to user_detail.html in Sub-Spec 1b (Task 6/7).
    """
    target = User.objects.create_user(username="hans", password="x", email="hans@x")
    target.membership_level = User.MembershipLevel.MEMBER
    target.save(update_fields=["membership_level"])

    client.force_login(admin_user)
    response = client.get(reverse("accounts:user_detail", args=[target.pk]))
    assert response.status_code == 200
    body = response.content.decode()
    # Section header + the dropdown
    assert "Vereins-Rolle" in body or "Vereinsrolle" in body
    assert "<select" in body
    # All four membership-level options should appear by display label
    assert "Vereins-Bewerber" in body
    assert "Vereins-Mitglied" in body
    assert "Vereins-Staff" in body
    assert "Vereins-Admin" in body


# NOTE: The old "test_user_form_does_not_render_membership_card_on_self" test
# (admin viewing own user_edit page must not show membership picker) was
# removed as part of Sub-Spec 1b/Task 7. UI cards moved to user_detail.html.
# The membership picker is now rendered in readonly mode (label-only, no
# form) when an admin views their own detail page — the writable picker is
# gated on `object.pk != request.user.pk` (see user_detail.html topology
# branch + test_admin_self_view_membership_picker_is_readonly in
# tests/test_user_detail_view.py). The server-side self-demote guard in
# MembershipSetView (test_self_demote_blocked above) remains the
# authoritative source of truth.
