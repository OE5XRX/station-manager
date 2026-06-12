"""Tests for apps/accounts/visibility.py (Sub-Spec 1a Foundation)."""

import pytest

from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def staff(db):
    return User.objects.create_user(
        username="OE5STAFF",
        password="x",
        membership_level=User.MembershipLevel.STAFF,
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


@pytest.fixture
def other_applicant(db):
    return User.objects.create_user(
        username="OE5BEW2",
        password="x",
        membership_level=User.MembershipLevel.APPLICANT,
    )


@pytest.mark.django_db
class TestAudienceFor:
    """audience_for(viewer, target) returns the right Audience tier."""

    def test_admin_sees_other_member_as_admin(self, admin, member):
        from apps.accounts.visibility import Audience, audience_for

        assert audience_for(admin, member) == Audience.ADMIN

    def test_admin_sees_applicant_as_admin(self, admin, applicant):
        from apps.accounts.visibility import Audience, audience_for

        assert audience_for(admin, applicant) == Audience.ADMIN

    def test_admin_sees_self_as_self(self, admin):
        """Admin sieht sich selbst zwar im SELF-Sinn, weil viewer.pk==target.pk
        Vorrang vor is_admin haben sollte — oder umgekehrt? Spec sagt: Admin-Check
        zuerst (Admin sieht sich als Admin)."""
        from apps.accounts.visibility import Audience, audience_for

        # Per Spec: viewer.is_admin precedes viewer==target check.
        assert audience_for(admin, admin) == Audience.ADMIN

    def test_member_sees_self_as_self(self, member):
        from apps.accounts.visibility import Audience, audience_for

        assert audience_for(member, member) == Audience.SELF

    def test_member_sees_other_member_as_member(self, member, other_member):
        from apps.accounts.visibility import Audience, audience_for

        assert audience_for(member, other_member) == Audience.MEMBER

    def test_member_sees_applicant_returns_none(self, member, applicant):
        from apps.accounts.visibility import audience_for

        assert audience_for(member, applicant) is None

    def test_applicant_sees_self_as_applicant(self, applicant):
        from apps.accounts.visibility import Audience, audience_for

        assert audience_for(applicant, applicant) == Audience.APPLICANT

    def test_applicant_sees_other_applicant_returns_none(self, applicant, other_applicant):
        from apps.accounts.visibility import audience_for

        assert audience_for(applicant, other_applicant) is None

    def test_applicant_sees_member_returns_none(self, applicant, member):
        from apps.accounts.visibility import audience_for

        assert audience_for(applicant, member) is None

    def test_anonymous_returns_none(self, member):
        from django.contrib.auth.models import AnonymousUser

        from apps.accounts.visibility import audience_for

        assert audience_for(AnonymousUser(), member) is None

    def test_staff_sees_member_as_member(self, staff, member):
        """Staff ist nicht is_admin (per User.is_admin property → nur Vereins-Admin).
        Daher behandelt audience_for() Staff wie einen normalen Member."""
        from apps.accounts.visibility import Audience, audience_for

        assert audience_for(staff, member) == Audience.MEMBER


@pytest.mark.django_db
class TestDirectoryVisibleFields:
    """directory_visible_fields(viewer, target) returns the right set."""

    def test_admin_sees_public_private_and_admin_only(self, admin, member):
        from apps.accounts.visibility import (
            ADMIN_ONLY_FIELDS,
            PRIVATE_PROFILE_FIELDS,
            PUBLIC_PROFILE_FIELDS,
            directory_visible_fields,
        )

        fields = directory_visible_fields(admin, member)
        assert fields >= PUBLIC_PROFILE_FIELDS
        assert fields >= PRIVATE_PROFILE_FIELDS
        assert fields >= ADMIN_ONLY_FIELDS

    def test_self_sees_public_and_private(self, member):
        from apps.accounts.visibility import (
            PRIVATE_PROFILE_FIELDS,
            PUBLIC_PROFILE_FIELDS,
            directory_visible_fields,
        )

        fields = directory_visible_fields(member, member)
        assert fields >= PUBLIC_PROFILE_FIELDS
        assert fields >= PRIVATE_PROFILE_FIELDS

    def test_self_sees_own_sso_sessions(self, member):
        from apps.accounts.visibility import directory_visible_fields

        fields = directory_visible_fields(member, member)
        assert "sso_sessions_self" in fields

    def test_self_does_not_see_admin_only_fields(self, member):
        from apps.accounts.visibility import (
            ADMIN_ONLY_FIELDS,
            directory_visible_fields,
        )

        fields = directory_visible_fields(member, member)
        # No admin-only sub-overlap (apart from sso_sessions_self which is
        # explicitly not in ADMIN_ONLY_FIELDS — siehe Sektion 4.3 spec).
        for f in ADMIN_ONLY_FIELDS:
            assert f not in fields, f"unexpected admin-only field {f} in self set"

    def test_self_sees_is_active_and_last_login(self, member):
        """Self soll eigenen is_active und last_login sehen (Sub-Spec 1a v2)."""
        from apps.accounts.visibility import directory_visible_fields

        fields = directory_visible_fields(member, member)
        assert "is_active" in fields
        assert "last_login" in fields

    def test_member_sees_other_member_public_only(self, member, other_member):
        from apps.accounts.visibility import (
            PUBLIC_PROFILE_FIELDS,
            directory_visible_fields,
        )

        # default: target is_directory_visible=True
        fields = directory_visible_fields(member, other_member)
        assert fields == PUBLIC_PROFILE_FIELDS

    def test_member_sees_invisible_member_minimal(self, member, other_member):
        from apps.accounts.visibility import (
            MINIMAL_DIRECTORY_FIELDS,
            directory_visible_fields,
        )

        other_member.is_directory_visible = False
        other_member.save()
        fields = directory_visible_fields(member, other_member)
        assert fields == MINIMAL_DIRECTORY_FIELDS

    def test_no_access_returns_empty(self, applicant, member):
        from apps.accounts.visibility import directory_visible_fields

        # Applicant sieht Member nicht
        fields = directory_visible_fields(applicant, member)
        assert fields == frozenset()


@pytest.mark.django_db
class TestUserCanViewDirectory:
    """user_can_view_directory(viewer) gates the UserListView."""

    def test_admin_can(self, admin):
        from apps.accounts.visibility import user_can_view_directory

        assert user_can_view_directory(admin) is True

    def test_member_can(self, member):
        from apps.accounts.visibility import user_can_view_directory

        assert user_can_view_directory(member) is True

    def test_staff_can(self, staff):
        from apps.accounts.visibility import user_can_view_directory

        assert user_can_view_directory(staff) is True

    def test_applicant_cannot(self, applicant):
        from apps.accounts.visibility import user_can_view_directory

        assert user_can_view_directory(applicant) is False

    def test_anonymous_cannot(self):
        from django.contrib.auth.models import AnonymousUser

        from apps.accounts.visibility import user_can_view_directory

        assert user_can_view_directory(AnonymousUser()) is False
