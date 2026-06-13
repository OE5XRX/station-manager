"""ProfileView rewrite: 4 forms with form_name dispatch.

Sub-Spec 1c Sektion 4.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        first_name="",
        last_name="",
        email="m@example.org",
        language="en",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestProfileViewGET:
    def test_get_renders_four_forms(self, client, member):
        client.force_login(member)
        resp = client.get(reverse("accounts:profile"))
        assert resp.status_code == 200
        for key in ("identity_form", "profile_form", "address_form", "password_form"):
            assert key in resp.context

    def test_get_has_onboarding_hints(self, client, member):
        client.force_login(member)
        resp = client.get(reverse("accounts:profile"))
        assert "onboarding_hints" in resp.context
        assert resp.context["onboarding_hints"]["name_missing"] is True

    def test_get_anonymous_redirected(self, client):
        resp = client.get(reverse("accounts:profile"))
        assert resp.status_code in (302, 401, 403)


@pytest.mark.django_db
class TestProfileViewPOSTIdentity:
    def test_identity_save(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": member.email,
                "identity-first_name": "Hans",
                "identity-last_name": "Müller",
                "identity-language": "en",
            },
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.first_name == "Hans"

    def test_identity_save_emits_audit(self, client, member):
        client.force_login(member)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).count()
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": member.email,
                "identity-first_name": "Hans",
                "identity-last_name": "",
                "identity-language": "en",
            },
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).count()
        assert after == before + 1
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).latest("created_at")
        assert "self-edit" in entry.message
        assert entry.actor == member


@pytest.mark.django_db
class TestProfileViewPOSTProfile:
    def test_profile_save(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:profile"),
            {
                "form_name": "profile",
                "profile-bio": "QRP enthusiast, 40m CW.",
                "profile-qth_name": "Linz",
                "profile-qrz_url": "",
                "profile-phone": "",
                "profile-is_directory_visible": "on",
            },
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.bio == "QRP enthusiast, 40m CW."
        assert member.qth_name == "Linz"


@pytest.mark.django_db
class TestProfileViewPOSTAddress:
    def test_address_save_no_geocode_when_unchanged(self, client, member):
        member.address = "Unchanged"
        member.save()
        client.force_login(member)
        resp = client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "Unchanged",
                "address-locator": "",
            },
        )
        assert resp.status_code == 302


@pytest.mark.django_db
class TestProfileViewPOSTUnknownForm:
    def test_unknown_form_name_redirects_with_error(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:profile"),
            {"form_name": "bogus"},
        )
        assert resp.status_code == 302
