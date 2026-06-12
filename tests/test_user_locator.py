"""Tests for apps/accounts/geocoding.lat_lon_to_locator (Sub-Spec 1a)."""

from decimal import Decimal

from apps.accounts.geocoding import lat_lon_to_locator


class TestMaidenheadLocator:
    """Pure-Python Maidenhead grid locator computation."""

    def test_linz_returns_jn78dh(self):
        # Linz Hauptplatz: 48.30694° N, 14.28583° E
        assert lat_lon_to_locator(48.30694, 14.28583) == "JN78DH"

    def test_vienna_returns_jn88ee(self):
        # Wien Stephansdom: 48.2° N, 16.37° E
        assert lat_lon_to_locator(48.2, 16.37) == "JN88EE"

    def test_precision_4_returns_4chars(self):
        result = lat_lon_to_locator(48.30694, 14.28583, precision=4)
        assert len(result) == 4
        assert result == "JN78"

    def test_equator_zero_meridian(self):
        # (lat=0, lon=0) is the boundary of JJ00AA in some conventions
        result = lat_lon_to_locator(0, 0)
        # Field calculation: lon+180=180, lat+90=90 → (J9, J9) → "JJ"
        # 180/20=9 → 'J', 90/10=9 → 'J'
        assert result.startswith("JJ")

    def test_negative_latitude_works(self):
        # Sydney, AU: lat=-33.87, lon=151.21 → QF56OD area
        result = lat_lon_to_locator(-33.87, 151.21)
        assert result.startswith("QF")
        assert len(result) == 6

    def test_negative_longitude_works(self):
        # San Francisco: lat=37.77, lon=-122.42 → CM87 area
        result = lat_lon_to_locator(37.77, -122.42)
        assert result.startswith("CM")
        assert len(result) == 6

    def test_accepts_decimal_input(self):
        # Same as Linz but via Decimal
        result = lat_lon_to_locator(Decimal("48.30694"), Decimal("14.28583"))
        assert result == "JN78DH"

    def test_default_precision_is_6(self):
        result = lat_lon_to_locator(48.30694, 14.28583)
        assert len(result) == 6

    def test_boundary_north_east_stays_in_a_to_r(self):
        """At lat=90 / lon=180 the divmod result reaches 18, which would
        produce 'S' without the clamp. The clamp keeps the field within
        A-R as required by LOCATOR_REGEX.
        """
        result = lat_lon_to_locator(90.0, 180.0)
        # First two letters MUST be in A-R (LOCATOR_REGEX accepts only that range).
        assert "A" <= result[0] <= "R"
        assert "A" <= result[1] <= "R"
        # Concretely: clamped to (17, 17) → 'R', 'R'.
        assert result.startswith("RR")
