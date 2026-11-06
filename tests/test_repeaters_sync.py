"""ÖVSV sync: mapping, upsert, deactivation, error handling."""

import io
import json
import urllib.error
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command

from apps.repeaters.models import Repeater
from apps.repeaters.oevsv import OevsvError, fetch_oevsv, normalize
from apps.repeaters.sync import sync_repeaters

pytestmark = pytest.mark.django_db

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _load(name):
    return json.loads((FIX / name).read_text())


def _rows():
    return normalize(_load("oevsv_trx.json"), _load("oevsv_site.json"))


def _by_id(rows, uid):
    return next(r for r in rows if r["external_id"] == str(uid))


def _fake_urlopen(request, timeout=None):
    url = request.full_url
    name = "oevsv_site.json" if "/site" in url else "oevsv_trx.json"
    return io.BytesIO((FIX / name).read_bytes())


def test_maps_oe5xll_2m():
    r = _by_id(_rows(), 2119)
    assert r["callsign"] == "OE5XLL"
    assert r["output_frequency"] == Decimal("145.60000")
    assert r["input_frequency"] == Decimal("145.00000")
    assert (r["input_tone"], r["output_tone"]) == ("none", "none")
    assert r["locator"] == "JN78AJ"
    assert r["latitude"] == Decimal("48.400000")
    assert r["active"] is True


def test_ctcss_float_to_string_and_direction():
    assert _by_id(_rows(), 2163)["input_tone"] == "77.0"
    r = _by_id(_rows(), 2086)
    # ctcss_rx = tone the repeater listens for = what we transmit.
    assert (r["input_tone"], r["output_tone"]) == ("162.2", "88.5")


def test_equal_freqs_simplex():
    sync_repeaters(_rows(), NOW)
    assert Repeater.objects.get(external_id="3001").is_simplex


def test_row_without_frequencies_skipped_others_imported():
    rows = _rows()
    assert "3002" not in {r["external_id"] for r in rows}
    result = sync_repeaters(rows, NOW)
    assert result.created == 6
    assert not Repeater.objects.filter(external_id="3002").exists()


def test_missing_site_gives_null_coords():
    r = _by_id(_rows(), 3001)
    assert r["latitude"] is None and r["longitude"] is None and r["locator"] == ""
    assert _by_id(_rows(), 2086)["locator"] == ""


def test_upsert_idempotent():
    sync_repeaters(_rows(), NOW)
    result = sync_repeaters(_rows(), NOW)
    assert (result.created, result.updated, result.deactivated) == (0, 0, 0)


def test_update_changes_counted():
    sync_repeaters(_rows(), NOW)
    rows = _rows()
    _by_id(rows, 2119)["site_name"] = "Renamed"
    result = sync_repeaters(rows, NOW)
    assert result.updated == 1
    assert Repeater.objects.get(external_id="2119").site_name == "Renamed"


def test_manual_rows_untouched():
    manual = Repeater.objects.create(
        callsign="OE5MAN",
        output_frequency=Decimal("145.6"),
        input_frequency=Decimal("145.0"),
        source="manual",
    )
    sync_repeaters(_rows(), NOW)
    manual.refresh_from_db()
    assert manual.active and manual.source == "manual"


def test_missing_from_feed_deactivated():
    sync_repeaters(_rows(), NOW)
    rows = [r for r in _rows() if r["external_id"] != "2119"]
    result = sync_repeaters(rows, NOW)
    assert result.deactivated == 1
    assert not Repeater.objects.get(external_id="2119").active


def test_historic_deactivated():
    rows = _rows()
    assert _by_id(rows, 3003)["active"] is False
    sync_repeaters(rows, NOW)
    assert not Repeater.objects.get(external_id="3003").active
    # Previously active row turning historic counts as deactivated.
    rows = _rows()
    _by_id(rows, 2119)["active"] = False
    assert sync_repeaters(rows, NOW).deactivated == 1


def test_fetch_oevsv_joins():
    with mock.patch("apps.repeaters.oevsv.urllib.request.urlopen", _fake_urlopen):
        rows = fetch_oevsv()
    assert len(rows) == 6


def test_fetch_error_raises():
    with mock.patch(
        "apps.repeaters.oevsv.urllib.request.urlopen", side_effect=urllib.error.URLError("down")
    ):
        with pytest.raises(OevsvError):
            fetch_oevsv()


def test_fetch_error_keeps_data(caplog):
    sync_repeaters(_rows(), NOW)
    with mock.patch(
        "apps.repeaters.oevsv.urllib.request.urlopen", side_effect=urllib.error.URLError("down")
    ):
        call_command("sync_repeaters", stdout=io.StringIO(), stderr=io.StringIO())
    assert Repeater.objects.filter(active=True).count() == 5
    assert "fetch failed" in caplog.text


def test_command_syncs():
    out = io.StringIO()
    with mock.patch("apps.repeaters.oevsv.urllib.request.urlopen", _fake_urlopen):
        call_command("sync_repeaters", stdout=out)
    assert "created=6" in out.getvalue()
