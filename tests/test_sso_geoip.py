"""GeoIP wrapper: happy path with fixture, missing-DB fallback, bad-IP fallback."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

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


def _gzip_bytes(payload: bytes) -> bytes:
    import gzip
    import io
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(payload)
    return buf.getvalue()


class _FakeResp:
    """Minimal file-like stand-in for ``urllib.request.urlopen``'s response."""

    def __init__(self, payload):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, n=-1):
        if n < 0:
            data, self._p = self._p, b""
            return data
        data, self._p = self._p[:n], self._p[n:]
        return data


def test_update_geoip_db_writes_target_when_current_month_ok(settings, tmp_path):
    target = tmp_path / "dbip.mmdb"
    settings.GEOIP_DB_PATH = str(target)
    fake_gz_bytes = _gzip_bytes(FIXTURE.read_bytes())

    with patch(
        "apps.sso.management.commands.update_geoip_db.urllib.request.urlopen",
        return_value=_FakeResp(fake_gz_bytes),
    ):
        call_command("update_geoip_db")

    assert target.exists()
    assert target.stat().st_size > 0


def test_update_geoip_db_falls_back_to_previous_month_on_404(settings, tmp_path):
    import urllib.error
    target = tmp_path / "dbip.mmdb"
    settings.GEOIP_DB_PATH = str(target)
    fake_gz_bytes = _gzip_bytes(FIXTURE.read_bytes())

    call_count = {"n": 0}

    def fake_urlopen(url, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _FakeResp(fake_gz_bytes)

    with patch(
        "apps.sso.management.commands.update_geoip_db.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        call_command("update_geoip_db")

    assert call_count["n"] == 2  # current month tried, then previous
    assert target.exists()


def test_update_geoip_db_raises_when_both_months_404(settings, tmp_path):
    import urllib.error
    settings.GEOIP_DB_PATH = str(tmp_path / "dbip.mmdb")

    def fake_urlopen(url, *args, **kwargs):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with patch(
        "apps.sso.management.commands.update_geoip_db.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        with pytest.raises(CommandError):
            call_command("update_geoip_db")
