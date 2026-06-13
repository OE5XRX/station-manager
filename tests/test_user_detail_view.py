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


@pytest.mark.django_db
class TestUserDetailViewContextLoading:
    """Admin gets the full management context; Self gets only own helpers."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_context_has_membership_choices(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "membership_level_choices" in resp.context

    def test_admin_context_has_region_assignments(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "existing_region_assignments" in resp.context
        assert "available_regions" in resp.context

    def test_admin_context_has_station_assignments(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "existing_station_assignments" in resp.context
        assert "all_stations" in resp.context

    def test_admin_context_has_sso_helpers(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "app_grants_list" in resp.context
        assert "user_sessions" in resp.context
        assert "tag_entries" in resp.context

    def test_self_context_omits_admin_only(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        # Self does NOT need the admin-only management context
        assert "available_regions" not in resp.context
        assert "all_stations" not in resp.context
        assert "app_grants_list" not in resp.context
        assert "tag_entries" not in resp.context

    def test_self_context_has_own_sessions(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        # Self can see own sessions (for self-revoke)
        assert "user_sessions" in resp.context

    def test_member_context_minimal(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        # Cross-member view has no Admin or SSO helpers
        assert "available_regions" not in resp.context
        assert "app_grants_list" not in resp.context
        assert "user_sessions" not in resp.context
        assert "tag_entries" not in resp.context

    def test_assignment_pills_for_admin(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        # Pills available for the topology tab (read-only display)
        assert "region_assignment_pills" in resp.context
        assert "station_assignment_pills" in resp.context

    def test_assignment_pills_for_member_when_target_visible(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        # PUBLIC set includes "region_assignments" + "station_assignments"
        assert "region_assignment_pills" in resp.context
        assert "station_assignment_pills" in resp.context

    def test_no_assignment_pills_for_invisible_target(self, client, member, other_member):
        other_member.is_directory_visible = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(other_member))
        # MINIMAL_DIRECTORY_FIELDS does not include assignments → no pills
        assert "region_assignment_pills" not in resp.context
        assert "station_assignment_pills" not in resp.context


@pytest.mark.django_db
class TestUserDetailViewAuditEntries:
    """Audit-Tab entries — Admin + Self get them, Member does not."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_gets_audit_entries(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "user_audit_entries" in resp.context
        # Empty list is OK; the key must exist for the template tab.
        assert resp.context["user_audit_entries"] == [] or isinstance(
            resp.context["user_audit_entries"], list
        )

    def test_self_gets_own_audit_entries(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        assert "user_audit_entries" in resp.context

    def test_member_does_not_get_audit_entries(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        assert "user_audit_entries" not in resp.context

    def test_audit_entries_account_and_sso_merged(self, client, admin, member):
        """AccountAuditLog entries on target_user + SsoAuditLog entries
        on target_user or actor are merged and sorted by created_at desc.
        """
        from apps.accounts.models import AccountAuditLog
        from apps.sso.models import SsoAuditLog

        # Mix of entries
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=member,
            message="created",
        )
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
            target_user=member,
            message="login",
        )

        client.force_login(admin)
        resp = client.get(self.url(member))
        entries = resp.context["user_audit_entries"]
        assert len(entries) == 2
        # Each entry is a (category, log_obj) tuple
        categories = {cat for cat, _ in entries}
        assert categories == {"account", "sso"}


@pytest.mark.django_db
class TestUserDetailViewTemplateRendering:
    """High-level HTML smoke tests for the four audience modes."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_view_renders_4_tabs(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        body = resp.content.decode()
        assert 'data-tab="overview"' in body
        assert 'data-tab="topology"' in body
        assert 'data-tab="sso"' in body
        assert 'data-tab="audit"' in body

    def test_admin_view_has_edit_and_delete_buttons(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        body = resp.content.decode()
        assert reverse("accounts:user_edit", kwargs={"pk": member.pk}) in body
        assert reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}) in body

    def test_admin_self_view_omits_edit_delete(self, client, admin):
        """Admin viewing own detail page does NOT see Edit/Delete — self-edit
        goes through accounts:profile (1c), and self-delete is blocked."""
        client.force_login(admin)
        resp = client.get(self.url(admin))
        body = resp.content.decode()
        # The Detail-Page does not show self-Edit/Delete buttons (deferred to profile)
        assert reverse("accounts:user_edit", kwargs={"pk": admin.pk}) not in body
        assert reverse("accounts:user_soft_delete", kwargs={"pk": admin.pk}) not in body

    def test_self_view_has_profile_edit_action(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        body = resp.content.decode()
        assert reverse("accounts:profile") in body

    def test_member_view_renders_2_tabs(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        body = resp.content.decode()
        assert 'data-tab="overview"' in body
        assert 'data-tab="topology"' in body
        assert 'data-tab="sso"' not in body
        assert 'data-tab="audit"' not in body

    def test_member_view_hides_private_fields(self, client, member, other_member):
        other_member.phone = "+43 1 23456"
        other_member.address = "Geheimstraße 7"
        other_member.email = "secret@example.org"
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(other_member))
        body = resp.content.decode()
        # Phone and address must NOT appear for cross-member view.
        assert "+43 1 23456" not in body
        assert "Geheimstraße 7" not in body
        # Email is in PUBLIC_PROFILE_FIELDS, so it CAN show.

    def test_member_view_invisible_target_minimal(self, client, member, other_member):
        other_member.bio = "Should not appear"
        other_member.qth_name = "Should not appear either"
        other_member.is_directory_visible = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(other_member))
        body = resp.content.decode()
        assert "Should not appear" not in body
        # Membership pill and username still show
        assert other_member.username in body

    def test_member_view_invisible_target_hides_full_name(self, client, member, other_member):
        """Page-head subtitle must not leak the target's real name to a
        Member viewing an is_directory_visible=False profile (the spec
        MINIMAL set has no first_name/last_name)."""
        other_member.first_name = "Hans"
        other_member.last_name = "Müller"
        other_member.is_directory_visible = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(other_member))
        body = resp.content.decode()
        assert "Hans" not in body
        assert "Müller" not in body
        assert other_member.username in body

    def test_self_view_topology_cards_show_existing_assignments(self, client, member):
        """Self/Member views render the management cards in readonly mode,
        but the cards still need existing_region_assignments /
        existing_station_assignments to render the pills (otherwise the
        cards' empty-state 'No assignments yet' fires even when there
        are assignments)."""
        from apps.stations.models import Region, RegionAssignment

        region = Region.objects.create(name="Innviertel")
        RegionAssignment.objects.create(
            user=member,
            region=region,
            role=RegionAssignment.Role.MANAGER,
        )
        client.force_login(member)
        resp = client.get(self.url(member))
        body = resp.content.decode()
        # The Region pill should be in the rendered HTML on the topology tab
        assert "Innviertel" in body
        # And the "No assignments yet" empty-state should NOT fire
        assert "No Region-Manager assignments yet" not in body

    def test_admin_self_view_membership_picker_is_readonly(self, client, admin):
        """Admin viewing own detail page sees the membership pill but NO
        writable picker. Self-promote/demote is blocked server-side
        (MembershipSetView), so showing a writable picker would be
        misleading. Restores the contract from the pre-1b user_form.html
        test that was removed in Task 7."""
        client.force_login(admin)
        resp = client.get(self.url(admin))
        body = resp.content.decode()
        # The Membership-Card title renders (pill view), but the
        # "Set membership level" form does NOT.
        assert "Vereins-Rolle" in body
        assert "Set membership level" not in body
        # And the HTMX-POST endpoint URL is not rendered for self
        assert reverse("accounts:membership_set", kwargs={"pk": admin.pk}) not in body

    def test_admin_other_view_membership_picker_is_writable(self, client, admin, member):
        """Admin viewing a DIFFERENT user's detail page DOES see the
        writable membership picker. Sanity check against over-zealous
        readonly gating."""
        client.force_login(admin)
        resp = client.get(self.url(member))
        body = resp.content.decode()
        assert "Set membership level" in body
        assert reverse("accounts:membership_set", kwargs={"pk": member.pk}) in body

    def test_admin_self_view_region_station_cards_are_writable(self, client, admin):
        """Admin viewing own detail page CAN manage own Region/Station
        assignments (no server-side self-restriction on those endpoints).
        Only the Membership card is readonly on self-view.
        """
        client.force_login(admin)
        resp = client.get(self.url(admin))
        body = resp.content.decode()
        # Region-Assignments-Card endpoints visible
        assert (
            reverse("accounts:region_assignment_create", kwargs={"user_pk": admin.pk}) in body
            or "region-assignments-card" in body
        )
        # Station-Assignments-Card endpoints visible
        assert (
            reverse("accounts:station_assignment_create", kwargs={"user_pk": admin.pk}) in body
            or "station-assignments-card" in body
        )


@pytest.mark.django_db
class TestUserFormCardCleanup:
    """user_form.html no longer renders management cards."""

    def test_edit_form_omits_cards(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_edit", kwargs={"pk": member.pk}))
        body = resp.content.decode()
        # Cards have moved to user_detail.html.
        assert "membership-card" not in body
        assert "region-assignments-card" not in body
        assert "station-assignments-card" not in body
        assert "sso-grants-card" not in body
        assert "sessions-card" not in body
        assert "tags-card" not in body


@pytest.mark.django_db
class TestSuccessRedirects:
    """Create/Update redirect to the user_detail page of the affected user."""

    def test_create_redirects_to_detail(self, client, admin):
        client.force_login(admin)
        resp = client.post(
            reverse("accounts:user_create"),
            {
                "username": "OE5NEW1",
                "email": "new@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "password1": "abcDEF123!xyz",
                "password2": "abcDEF123!xyz",
            },
            follow=False,
        )
        assert resp.status_code == 302
        created = User.objects.get(username="OE5NEW1")
        assert resp.url == reverse("accounts:user_detail", kwargs={"pk": created.pk})

    def test_update_redirects_to_detail(self, client, admin, member):
        client.force_login(admin)
        resp = client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            {
                "username": member.username,
                "email": "updated@example.org",
                "first_name": "Updated",
                "last_name": "",
                "language": "en",
                "is_active": "on",
            },
            follow=False,
        )
        assert resp.status_code == 302
        assert resp.url == reverse("accounts:user_detail", kwargs={"pk": member.pk})
