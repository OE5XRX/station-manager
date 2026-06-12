"""Tests for RegionAssignment HTMX endpoints."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Region, RegionAssignment


def _user(level, username):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRegionAssignmentCreateView:
    def test_admin_can_add_region_manager(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:region_assignment_create", args=[lisa.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 200
        assert RegionAssignment.objects.filter(user=lisa, region=r).exists()

    def test_applicant_target_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        applicant = _user(User.MembershipLevel.APPLICANT, "newbie")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:region_assignment_create", args=[applicant.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 400
        assert not RegionAssignment.objects.filter(user=applicant).exists()

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(staff)
        response = client.post(
            reverse("accounts:region_assignment_create", args=[target.pk]),
            {"region": r.pk},
        )
        assert response.status_code in (302, 403)
        assert not RegionAssignment.objects.filter(user=target).exists()

    def test_invalid_region_returns_404(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:region_assignment_create", args=[target.pk]),
            {"region": "99999"},
        )
        assert response.status_code == 404

    def test_duplicate_assignment_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(user=lisa, region=r, role=RegionAssignment.Role.MANAGER)
        client.force_login(admin)
        response = client.post(
            reverse("accounts:region_assignment_create", args=[lisa.pk]),
            {"region": r.pk},
        )
        # uniq_user_role_per_region constraint catches it
        assert response.status_code == 400


@pytest.mark.django_db
class TestRegionAssignmentRevokeView:
    def test_admin_can_revoke(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=lisa, region=r, role=RegionAssignment.Role.MANAGER
        )
        client.force_login(admin)
        response = client.post(reverse("accounts:region_assignment_revoke", args=[a.pk]))
        assert response.status_code == 200
        assert not RegionAssignment.objects.filter(pk=a.pk).exists()

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=lisa, region=r, role=RegionAssignment.Role.MANAGER
        )
        client.force_login(staff)
        response = client.post(reverse("accounts:region_assignment_revoke", args=[a.pk]))
        assert response.status_code in (302, 403)
        assert RegionAssignment.objects.filter(pk=a.pk).exists()


@pytest.mark.django_db
class TestRegionAssignmentsCardRendering:
    def test_card_visible_to_admin_for_member(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        Region.objects.create(name="Tirol", slug="tirol")
        Region.objects.create(name="OOe", slug="ooe")
        client.force_login(admin)
        response = client.get(reverse("accounts:user_detail", args=[lisa.pk]))
        body = response.content.decode()
        assert response.status_code == 200
        assert "Region-Manager" in body
        # The select offers the two regions
        assert "Tirol" in body
        assert "OOe" in body

    def test_card_lists_existing_assignment_with_revoke_button(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=lisa, region=r, role=RegionAssignment.Role.MANAGER
        )
        client.force_login(admin)
        response = client.get(reverse("accounts:user_detail", args=[lisa.pk]))
        body = response.content.decode()
        # The revoke URL is rendered as the form target
        assert reverse("accounts:region_assignment_revoke", args=[a.pk]) in body

    def test_card_warns_for_applicant_target(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        applicant = _user(User.MembershipLevel.APPLICANT, "newbie")
        client.force_login(admin)
        response = client.get(reverse("accounts:user_detail", args=[applicant.pk]))
        body = response.content.decode()
        # The warning mentions the membership-level requirement
        assert "Vereins-Bewerber" in body or "applicant" in body.lower()
