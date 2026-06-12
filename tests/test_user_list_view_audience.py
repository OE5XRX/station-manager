"""UserListView audience-aware: dispatch, queryset filter, get-params."""

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
class TestUserListPermissions:
    def url(self):
        return reverse("accounts:user_list")

    def test_admin_can_access(self, client, admin):
        client.force_login(admin)
        resp = client.get(self.url())
        assert resp.status_code == 200

    def test_member_can_access(self, client, member):
        client.force_login(member)
        resp = client.get(self.url())
        assert resp.status_code == 200

    def test_applicant_cannot_access(self, client, applicant):
        client.force_login(applicant)
        resp = client.get(self.url())
        assert resp.status_code == 404

    def test_anonymous_redirected(self, client):
        resp = client.get(self.url())
        assert resp.status_code in (302, 401, 403)


@pytest.mark.django_db
class TestUserListAudienceFilter:
    """Member sees no applicants; Admin sees all by default."""

    def url(self, **params):
        u = reverse("accounts:user_list")
        if params:
            from urllib.parse import urlencode

            u += "?" + urlencode(params)
        return u

    def test_admin_sees_applicants(self, client, admin, member, applicant):
        client.force_login(admin)
        resp = client.get(self.url())
        usernames = [u.username for u in resp.context["users"]]
        assert applicant.username in usernames
        assert member.username in usernames

    def test_member_does_not_see_applicants(self, client, member, applicant, other_member):
        client.force_login(member)
        resp = client.get(self.url())
        usernames = [u.username for u in resp.context["users"]]
        assert applicant.username not in usernames
        assert other_member.username in usernames

    def test_role_filter_member(self, client, admin, member, other_member, applicant):
        client.force_login(admin)
        resp = client.get(self.url(role="member"))
        usernames = [u.username for u in resp.context["users"]]
        assert member.username in usernames
        assert other_member.username in usernames
        assert applicant.username not in usernames
        assert admin.username not in usernames

    def test_role_filter_applicant_admin_only(self, client, admin, member, applicant):
        client.force_login(admin)
        resp = client.get(self.url(role="applicant"))
        usernames = [u.username for u in resp.context["users"]]
        assert applicant.username in usernames
        assert member.username not in usernames

    def test_member_cannot_filter_to_applicants(self, client, member, applicant, other_member):
        """Even if a member tries ?role=applicant, the queryset excludes
        applicants because the audience filter applies first."""
        client.force_login(member)
        resp = client.get(self.url(role="applicant"))
        usernames = [u.username for u in resp.context["users"]]
        assert applicant.username not in usernames

    def test_search_filter_q(self, client, admin, member):
        member.email = "specialhandle@example.org"
        member.save()
        client.force_login(admin)
        resp = client.get(self.url(q="specialhandle"))
        usernames = [u.username for u in resp.context["users"]]
        assert member.username in usernames

    def test_admin_status_filter_inactive(self, client, admin, member, other_member):
        other_member.is_active = False
        other_member.save()
        client.force_login(admin)
        resp = client.get(self.url(status="inactive"))
        usernames = [u.username for u in resp.context["users"]]
        assert other_member.username in usernames
        assert member.username not in usernames

    def test_member_status_param_ignored(self, client, member, other_member):
        """Member tries ?status=inactive — the param is ignored (no admin)."""
        other_member.is_active = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(status="inactive"))
        usernames = [u.username for u in resp.context["users"]]
        # other_member.is_active=False but is_directory_visible=True →
        # still appears for member (status filter not applied).
        assert other_member.username in usernames


@pytest.mark.django_db
class TestUserListTemplate:
    def url(self, **params):
        u = reverse("accounts:user_list")
        if params:
            from urllib.parse import urlencode

            u += "?" + urlencode(params)
        return u

    def test_admin_sees_new_user_button(self, client, admin):
        client.force_login(admin)
        resp = client.get(self.url())
        body = resp.content.decode()
        assert reverse("accounts:user_create") in body

    def test_member_does_not_see_new_user_button(self, client, member):
        client.force_login(member)
        resp = client.get(self.url())
        body = resp.content.decode()
        assert reverse("accounts:user_create") not in body

    def test_member_sees_only_view_button(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url())
        body = resp.content.decode()
        assert reverse("accounts:user_detail", kwargs={"pk": other_member.pk}) in body
        assert reverse("accounts:user_edit", kwargs={"pk": other_member.pk}) not in body
        assert reverse("accounts:user_delete", kwargs={"pk": other_member.pk}) not in body

    def test_admin_sees_view_edit_delete(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url())
        body = resp.content.decode()
        assert reverse("accounts:user_detail", kwargs={"pk": member.pk}) in body
        assert reverse("accounts:user_edit", kwargs={"pk": member.pk}) in body
        assert reverse("accounts:user_delete", kwargs={"pk": member.pk}) in body

    def test_filter_bar_role_options_admin(self, client, admin):
        client.force_login(admin)
        resp = client.get(self.url())
        body = resp.content.decode()
        # Admin sees Applicant option in role select
        assert "applicant" in body.lower() or "Bewerber" in body

    def test_member_view_hides_invisible_other_fields(self, client, member, other_member):
        other_member.email = "secret@example.org"
        other_member.is_directory_visible = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url())
        body = resp.content.decode()
        # Email of an invisible member must NOT show up in the list table
        assert "secret@example.org" not in body
