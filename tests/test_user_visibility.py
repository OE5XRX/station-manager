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
