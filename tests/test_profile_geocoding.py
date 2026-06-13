"""ProfileView address-save → geocoding trigger.

Sub-Spec 1c Sektion 4 _maybe_geocode.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestProfileAddressGeocoding:
    @patch("apps.accounts.views.geocode_address")
    def test_address_change_triggers_geocode(self, mock_geocode, client, member):
        mock_geocode.return_value = (Decimal("48.3"), Decimal("14.3"))
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "Hauptstraße 1, 4020 Linz",
                "address-locator": "",
            },
        )
        mock_geocode.assert_called_once()
        member.refresh_from_db()
        assert member.latitude == Decimal("48.3")
        assert member.locator.startswith("JN")

    @patch("apps.accounts.views.geocode_address")
    def test_address_cleared_resets_coords(self, mock_geocode, client, member):
        member.address = "Old"
        member.latitude = Decimal("48.3")
        member.longitude = Decimal("14.3")
        member.locator = "JN78AB"
        member.save()
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "",
                "address-locator": "",
            },
        )
        member.refresh_from_db()
        assert member.latitude is None
        assert member.locator == ""
        mock_geocode.assert_not_called()

    @patch("apps.accounts.views.geocode_address")
    def test_geocode_failure_leaves_coords(self, mock_geocode, client, member):
        mock_geocode.return_value = None
        member.latitude = Decimal("48.3")
        member.longitude = Decimal("14.3")
        member.locator = "JN78AB"
        member.save()
        client.force_login(member)
        # The browser-rendered form pre-populates the locator input from the
        # instance, so a POST that only changes address sends the existing
        # locator value back unchanged. Simulate that here.
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "Geocoding will fail for this",
                "address-locator": "JN78AB",
            },
        )
        member.refresh_from_db()
        # Coords stay even though geocode returned None — the spec says
        # "fail closed: leave existing values, user can manual-override".
        assert member.latitude == Decimal("48.3")
        assert member.locator == "JN78AB"

    @patch("apps.accounts.views.geocode_address")
    def test_geocode_failure_honors_manual_locator_override(self, mock_geocode, client, member):
        """If user explicitly types a new locator in the same submit as a
        failing address, the manual override wins (no restore)."""
        mock_geocode.return_value = None
        member.locator = "JN78AB"
        member.save()
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "Geocoding will fail for this",
                "address-locator": "JO45AB",
            },
        )
        member.refresh_from_db()
        # User's typed override wins even though geocode failed.
        assert member.locator == "JO45AB"
