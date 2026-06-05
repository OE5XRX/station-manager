"""Tests for User.membership_level field + helpers.

The membership_level is the new vereinsweit role indicator on User.
Replaces today's group-based admin/operator/member detection.
"""

import pytest

from apps.accounts.models import User


@pytest.mark.django_db
def test_membership_level_default_is_applicant():
    """A freshly-created user without explicit level lands on APPLICANT.

    Production today does not have a self-service signup, but the
    APPLICANT default is the safe fallback for SSO-bootstrapped users
    and the future signup flow.
    """
    user = User.objects.create_user(username="nobody", password="x")
    assert user.membership_level == User.MembershipLevel.APPLICANT


@pytest.mark.django_db
def test_membership_level_choices_exist():
    """All four level values are defined as TextChoices."""
    assert User.MembershipLevel.APPLICANT == "applicant"
    assert User.MembershipLevel.MEMBER == "member"
    assert User.MembershipLevel.STAFF == "staff"
    assert User.MembershipLevel.ADMIN == "admin"


@pytest.mark.django_db
def test_membership_level_display_labels():
    """Display labels use the 'Vereins-X' compound form."""
    user = User.objects.create_user(username="u", password="x")
    user.membership_level = User.MembershipLevel.STAFF
    user.save(update_fields=["membership_level"])
    assert user.get_membership_level_display() == "Vereins-Staff"
