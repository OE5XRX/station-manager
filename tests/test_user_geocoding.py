"""Tests for apps/accounts/geocoding.geocode_address (Sub-Spec 1a Foundation).

Geocoding via Nominatim/OSM — wir mocken requests.get, kein echter
HTTP-Call im Test.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests

from apps.accounts.geocoding import geocode_address


class TestGeocodeAddress:
    """geocode_address(address) returns (Decimal, Decimal) or None."""

    @patch("apps.accounts.geocoding.time.sleep")  # rate-limit umgehen im Test
    @patch("apps.accounts.geocoding.requests.get")
    def test_valid_response_returns_decimal_tuple(self, mock_get, _mock_sleep):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"lat": "48.30694", "lon": "14.28583", "display_name": "Linz, Austria"}
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = geocode_address("Hauptstraße 1, 4020 Linz")
        assert result is not None
        lat, lon = result
        assert lat == Decimal("48.30694")
        assert lon == Decimal("14.28583")

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_user_agent_header_is_set(self, mock_get, _mock_sleep):
        mock_response = MagicMock()
        mock_response.json.return_value = [{"lat": "0", "lon": "0"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        geocode_address("Any address")

        call_kwargs = mock_get.call_args.kwargs
        headers = call_kwargs["headers"]
        assert "User-Agent" in headers
        assert "OE5XRX" in headers["User-Agent"]

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_empty_address_returns_none_without_http_call(self, mock_get, _mock_sleep):
        assert geocode_address("") is None
        assert geocode_address("   ") is None
        assert geocode_address(None) is None
        mock_get.assert_not_called()

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_no_result_returns_none(self, mock_get, _mock_sleep):
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        assert geocode_address("Nonsense location") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_http_error_returns_none(self, mock_get, _mock_sleep):
        mock_get.side_effect = requests.HTTPError("500 Server Error")

        assert geocode_address("Hauptstraße 1, 4020 Linz") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_timeout_returns_none(self, mock_get, _mock_sleep):
        mock_get.side_effect = requests.Timeout("Connection timed out")

        assert geocode_address("Hauptstraße 1, 4020 Linz") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_malformed_response_returns_none(self, mock_get, _mock_sleep):
        # Antwort fehlen lat/lon-Keys
        mock_response = MagicMock()
        mock_response.json.return_value = [{"some_other_field": "value"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        assert geocode_address("Hauptstraße 1, 4020 Linz") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_rate_limit_sleep_invoked(self, mock_get, mock_sleep):
        mock_response = MagicMock()
        mock_response.json.return_value = [{"lat": "0", "lon": "0"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        geocode_address("Some place")
        mock_sleep.assert_called_once_with(1)
