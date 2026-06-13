"""USER_UPDATED / USER_CREATED / USER_ACTIVATED / USER_DEACTIVATED
audit emission from UserUpdateView + UserCreateView, plus
geocoding-trigger on address change.

Sub-Spec 1c Sektion 5 + 6.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User


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
        first_name="Hans",
        last_name="Müller",
        email="hans@example.org",
        language="en",
        membership_level=User.MembershipLevel.MEMBER,
    )


def _form_payload(member, **overrides):
    base = {
        "username": member.username,
        "email": member.email,
        "first_name": member.first_name,
        "last_name": member.last_name,
        "language": member.language,
        "is_active": "on" if member.is_active else "",
        "bio": member.bio,
        "qth_name": member.qth_name,
        "qrz_url": member.qrz_url,
        "phone": member.phone,
        "address": member.address,
        "locator": member.locator,
        "is_directory_visible": "on" if member.is_directory_visible else "",
    }
    base.update(overrides)
    return base


@pytest.mark.django_db
class TestUserUpdateViewAudit:
    def test_identity_change_emits_user_updated(self, client, admin, member):
        client.force_login(admin)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED,
            target_user=member,
        ).count()
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, email="new@example.org"),
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED,
            target_user=member,
        ).count()
        assert after == before + 1
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).latest("created_at")
        assert "email" in entry.message
        assert entry.actor == admin

    def test_no_change_no_audit(self, client, admin, member):
        client.force_login(admin)
        before = AccountAuditLog.objects.filter(target_user=member).count()
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member),
        )
        after = AccountAuditLog.objects.filter(target_user=member).count()
        # No changed_data → no audit
        assert after == before

    def test_is_active_flip_emits_deactivated(self, client, admin, member):
        client.force_login(admin)
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, is_active=""),
        )
        member.refresh_from_db()
        assert not member.is_active
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_DEACTIVATED,
            target_user=member,
        ).latest("created_at")
        assert entry.actor == admin

    def test_is_active_only_emits_only_deactivated_not_updated(self, client, admin, member):
        client.force_login(admin)
        before_updated = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).count()
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, is_active=""),
        )
        after_updated = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).count()
        # is_active alone → no USER_UPDATED
        assert after_updated == before_updated


@pytest.mark.django_db
class TestUserUpdateViewGeocodingTrigger:
    @patch("apps.accounts.views.geocode_address")
    def test_address_change_calls_geocode(self, mock_geocode, client, admin, member):
        mock_geocode.return_value = (Decimal("48.3"), Decimal("14.3"))
        client.force_login(admin)
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, address="Hauptstraße 1, 4020 Linz"),
        )
        mock_geocode.assert_called_once()
        member.refresh_from_db()
        assert member.latitude == Decimal("48.3")
        assert member.longitude == Decimal("14.3")
        # Locator was computed from coords
        assert member.locator.startswith("JN")

    @patch("apps.accounts.views.geocode_address")
    def test_address_unchanged_no_geocode(self, mock_geocode, client, admin, member):
        client.force_login(admin)
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, email="new@example.org"),
        )
        mock_geocode.assert_not_called()

    @patch("apps.accounts.views.geocode_address")
    def test_address_cleared_clears_coords(self, mock_geocode, client, admin, member):
        member.address = "Old address"
        member.latitude = Decimal("48.3")
        member.longitude = Decimal("14.3")
        member.locator = "JN78AB"
        member.save()
        client.force_login(admin)
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, address=""),
        )
        member.refresh_from_db()
        assert member.address == ""
        assert member.latitude is None
        assert member.longitude is None
        # Locator follows the address when not explicitly overridden
        assert member.locator == ""
        mock_geocode.assert_not_called()


@pytest.mark.django_db
class TestUserCreateViewAudit:
    def test_create_emits_user_created(self, client, admin):
        client.force_login(admin)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_CREATED
        ).count()
        client.post(
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
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_CREATED
        ).count()
        assert after == before + 1
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_CREATED
        ).latest("created_at")
        assert "OE5NEW1" in entry.message
        assert entry.actor == admin
