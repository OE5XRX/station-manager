"""GeoIP wrapper: happy path with fixture, missing-DB fallback, bad-IP fallback."""

from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "dbip-city-lite-test.mmdb"


def _reset_singleton():
    """Force the geoip module to re-read settings on next call."""
    from apps.sso import geoip
    geoip._reader = None
    geoip._reader_load_failed = False


def test_lookup_known_ip_returns_country_and_city(settings, tmp_path):
    target = tmp_path / "test.mmdb"
    target.write_bytes(FIXTURE.read_bytes())
    settings.GEOIP_DB_PATH = str(target)
    _reset_singleton()
    from apps.sso.geoip import lookup_location
    country, city = lookup_location("89.207.4.5")
    assert country == "AT"
    assert city == "Linz"


def test_lookup_unknown_ip_returns_none(settings, tmp_path):
    target = tmp_path / "test.mmdb"
    target.write_bytes(FIXTURE.read_bytes())
    settings.GEOIP_DB_PATH = str(target)
    _reset_singleton()
    from apps.sso.geoip import lookup_location
    assert lookup_location("203.0.113.1") == (None, None)


def test_lookup_when_db_missing_returns_none(settings, tmp_path):
    settings.GEOIP_DB_PATH = str(tmp_path / "does-not-exist.mmdb")
    _reset_singleton()
    from apps.sso.geoip import lookup_location
    assert lookup_location("89.207.4.5") == (None, None)


def test_lookup_with_none_ip_returns_none(settings):
    _reset_singleton()
    from apps.sso.geoip import lookup_location
    assert lookup_location(None) == (None, None)
    assert lookup_location("") == (None, None)
