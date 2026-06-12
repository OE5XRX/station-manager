"""Permission matrix for UserDetailView (Sub-Spec 1b).

Audience tiers come from apps/accounts/visibility.py:
  - Admin sees everyone
  - Self/Applicant sees own detail page
  - Member sees other members (not applicants) when target.is_directory_visible
  - Member sees invisible-target reduced to MINIMAL fields
"""

import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def other_member(db):
    return User.objects.create_user(
        username="OE5MEM2",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def applicant(db):
    return User.objects.create_user(
        username="OE5BEW1",
        password="x",
        membership_level=User.MembershipLevel.APPLICANT,
    )


@pytest.mark.django_db
class TestUserDetailViewPermissions:
    """Each request returns 200 / 404 based on Audience tier."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_sees_any_user(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert resp.status_code == 200

    def test_admin_sees_applicant(self, client, admin, applicant):
        client.force_login(admin)
        resp = client.get(self.url(applicant))
        assert resp.status_code == 200

    def test_member_sees_other_member(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        assert resp.status_code == 200

    def test_member_sees_own_detail(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        assert resp.status_code == 200

    def test_member_cannot_see_applicant(self, client, member, applicant):
        client.force_login(member)
        resp = client.get(self.url(applicant))
        assert resp.status_code == 404

    def test_applicant_sees_own_detail(self, client, applicant):
        client.force_login(applicant)
        resp = client.get(self.url(applicant))
        assert resp.status_code == 200

    def test_applicant_cannot_see_member(self, client, applicant, member):
        client.force_login(applicant)
        resp = client.get(self.url(member))
        assert resp.status_code == 404

    def test_anonymous_redirected_to_login(self, client, member):
        # No login → LoginRequiredMixin redirects (302) to LOGIN_URL.
        resp = client.get(self.url(member))
        assert resp.status_code in (302, 401, 403)


@pytest.mark.django_db
class TestUserDetailViewAudienceFlags:
    """Context exposes audience-aware booleans for the template."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_view_flag(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        ctx = resp.context
        assert ctx["is_admin_view"] is True
        assert ctx["is_self_view"] is False
        assert ctx["is_member_view"] is False

    def test_self_view_flag(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        ctx = resp.context
        assert ctx["is_admin_view"] is False
        assert ctx["is_self_view"] is True
        assert ctx["is_member_view"] is False

    def test_member_view_flag(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        ctx = resp.context
        assert ctx["is_admin_view"] is False
        assert ctx["is_self_view"] is False
        assert ctx["is_member_view"] is True

    def test_visible_fields_set_in_context(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        assert "visible_fields" in resp.context
        # Member sees PUBLIC fields of a directory-visible target.
        assert "username" in resp.context["visible_fields"]
        # Member does NOT see private fields of other members.
        assert "phone" not in resp.context["visible_fields"]
