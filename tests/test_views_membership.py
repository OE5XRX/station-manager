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
