"""Tests for apps/accounts/geocoding.geocode_address (Sub-Spec 1a Foundation).

Geocoding via Nominatim/OSM — wir mocken requests.get, kein echter
HTTP-Call im Test.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.test import override_settings

from apps.accounts.geocoding import DEFAULT_USER_AGENT, geocode_address


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

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_user_agent_default_is_generic(self, mock_get, _mock_sleep):
        """Default User-Agent carries the project name only, no personal email."""
        mock_response = MagicMock()
        mock_response.json.return_value = [{"lat": "0", "lon": "0"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        geocode_address("Any address")
        ua = mock_get.call_args.kwargs["headers"]["User-Agent"]
        assert ua == DEFAULT_USER_AGENT
        # PII guard: a personal email must NOT show up in the default UA.
        assert "@" not in ua

    @override_settings(NOMINATIM_USER_AGENT="MyClub/2.0 (admin@example.org)")
    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_user_agent_setting_overrides_default(self, mock_get, _mock_sleep):
        """Deployments can inject contact info via NOMINATIM_USER_AGENT."""
        mock_response = MagicMock()
        mock_response.json.return_value = [{"lat": "0", "lon": "0"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        geocode_address("Any address")
        ua = mock_get.call_args.kwargs["headers"]["User-Agent"]
        assert ua == "MyClub/2.0 (admin@example.org)"

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_invalid_decimal_in_response_returns_none(self, mock_get, _mock_sleep):
        """Nominatim returning a non-numeric lat/lon must not propagate
        InvalidOperation — the function fails closed to None.
        """
        mock_response = MagicMock()
        mock_response.json.return_value = [{"lat": "not_a_number", "lon": "0"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        assert geocode_address("Some place") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_null_lat_in_response_returns_none(self, mock_get, _mock_sleep):
        """Nominatim returning lat/lon=null triggers TypeError on Decimal(None),
        which must be caught and fail closed.
        """
        mock_response = MagicMock()
        mock_response.json.return_value = [{"lat": None, "lon": "0"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        assert geocode_address("Some place") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_failure_log_does_not_leak_address(self, mock_get, _mock_sleep, caplog):
        """Privacy: the warning log on geocode failure must not contain
        the raw address (PII)."""
        import logging

        mock_get.side_effect = requests.HTTPError("500 Server Error")

        sensitive = "Geheimstraße 7, 4020 Linz"
        with caplog.at_level(logging.WARNING, logger="apps.accounts.geocoding"):
            result = geocode_address(sensitive)
        assert result is None
        # The address itself must not appear anywhere in the log records.
        for record in caplog.records:
            assert sensitive not in record.getMessage()
            assert "Geheimstraße" not in record.getMessage()

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_failure_log_does_not_leak_exception_message(self, mock_get, _mock_sleep, caplog):
        """Privacy: ``str(exc)`` from ``requests.HTTPError`` and friends
        interpolates the full request URL — which contains the address
        as the ``q=`` query parameter. The log MUST NOT include the
        exception's string form, only its class name (and optionally the
        HTTP status as safe metadata).
        """
        import logging

        sensitive = "Geheimstraße 7, 4020 Linz"
        # Build an HTTPError whose str() contains the URL with the
        # address — this mirrors what requests' raise_for_status produces.
        url_with_address = (
            "500 Server Error: Internal Server Error for url: "
            "https://nominatim.openstreetmap.org/search?q=Geheimstra%C3%9Fe+7"
        )
        mock_get.side_effect = requests.HTTPError(url_with_address)

        with caplog.at_level(logging.WARNING, logger="apps.accounts.geocoding"):
            result = geocode_address(sensitive)
        assert result is None
        # Neither the raw address nor the URL-encoded form may leak.
        for record in caplog.records:
            msg = record.getMessage()
            assert "Geheimstra" not in msg, f"raw address leaked: {msg!r}"
            assert "q=" not in msg, f"URL with query param leaked: {msg!r}"
            assert "nominatim.openstreetmap.org/search?" not in msg, (
                f"URL with query string leaked: {msg!r}"
            )

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_failure_log_includes_http_status_when_available(self, mock_get, _mock_sleep, caplog):
        """HTTP status is safe metadata and useful for operator triage —
        log it when a response is attached to the exception.
        """
        import logging

        mock_response = MagicMock()
        mock_response.status_code = 503
        http_error = requests.HTTPError("Service Unavailable")
        http_error.response = mock_response
        mock_get.side_effect = http_error

        with caplog.at_level(logging.WARNING, logger="apps.accounts.geocoding"):
            geocode_address("Some place")
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "503" in joined
        assert "HTTPError" in joined
