"""Smoke test for membership-level pill in user_list."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_user_list_renders_membership_level_pills(
    client, admin_user, member_user, operator_user, applicant_user
):
    """Each user row shows the Vereins-X pill from membership_level."""
    client.force_login(admin_user)
    response = client.get(reverse("accounts:user_list"))

    assert response.status_code == 200
    body = response.content.decode()

    # Pill labels (TextChoices display values)
    assert "Vereins-Admin" in body
    assert "Vereins-Mitglied" in body
    assert "Vereins-Staff" in body
    assert "Vereins-Bewerber" in body


@pytest.mark.django_db
def test_user_list_sub_text_says_new_terminology(client, admin_user):
    """The page sub-text uses the new membership-level terminology."""
    client.force_login(admin_user)
    response = client.get(reverse("accounts:user_list"))

    body = response.content.decode()
    assert "member, staff, and admin accounts" in body


@pytest.mark.django_db
def test_profile_page_renders_membership_pill(client, member_user):
    """Own profile page shows the user's membership-level pill."""
    client.force_login(member_user)
    response = client.get(reverse("accounts:profile"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Vereins-Mitglied" in body


@pytest.mark.django_db
def test_user_confirm_soft_delete_renders_membership_pill(client, admin_user, member_user):
    """The soft-delete confirm page shows the target user's membership-level pill."""
    client.force_login(admin_user)
    response = client.get(reverse("accounts:user_soft_delete", args=[member_user.pk]))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Vereins-Mitglied" in body
