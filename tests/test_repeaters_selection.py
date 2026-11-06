"""Station-side repeater selection: bands, distance, cross-band."""

from decimal import Decimal

import pytest

from apps.control.models import StationModule
from apps.repeaters.models import Repeater
from apps.repeaters.selection import (
    band_of,
    bearing_label,
    haversine_km,
    repeaters_for_station,
    station_bands,
)

pytestmark = pytest.mark.django_db

VHF = ("vhf", 134, 174)
UHF = ("uhf", 400, 480)


def _descriptor(band):
    name, lo, hi = band
    caps = [
        {"name": "rx_frequency", "ranges": [{"name": name, "min": lo, "max": hi}]},
        {"name": "tx_frequency", "ranges": [{"name": name, "min": lo, "max": hi}]},
        {"name": "tx_tone"},
        {"name": "rx_tone"},
    ]
    return caps


def _module(station, slot, band, online=True, module_id="fm"):
    return StationModule.objects.create(
        station=station,
        slot=slot,
        module_id=module_id,
        online=online,
        capability_descriptor=_descriptor(band),
    )


def _rep(callsign, out, inp, **kw):
    return Repeater.objects.create(
        callsign=callsign,
        output_frequency=Decimal(out),
        input_frequency=Decimal(inp),
        source="manual",
        **kw,
    )


def test_filters_to_station_bands(station):
    _module(station, "1", VHF)
    _rep("TWO", "145.600", "145.000")
    _rep("SEVENTY", "438.650", "431.050")
    assert [r.repeater.callsign for r in repeaters_for_station(station)] == ["TWO"]


def test_sorted_by_distance(station):
    station.latitude, station.longitude = Decimal("48.0"), Decimal("14.0")
    station.save()
    _module(station, "1", VHF)
    _rep("FAR", "145.600", "145.000", latitude=Decimal("48.0"), longitude=Decimal("15.0"))
    _rep("NEAR", "145.625", "145.025", latitude=Decimal("48.0"), longitude=Decimal("14.1"))
    rows = repeaters_for_station(station)
    assert [r.repeater.callsign for r in rows] == ["NEAR", "FAR"]
    assert rows[0].bearing == "E"
    assert 7 < rows[0].distance_km < 8
    assert 70 < rows[1].distance_km < 75


def test_cross_band_flag_when_both_bands_present(station):
    _module(station, "1", VHF)
    _module(station, "2", UHF)
    _rep("XB", "145.600", "431.050")
    _rep("SAME", "145.650", "145.050")
    rows = {r.repeater.callsign: r for r in repeaters_for_station(station)}
    assert rows["XB"].cross_band
    assert (rows["XB"].rx_band, rows["XB"].tx_band) == ("vhf", "uhf")
    assert not rows["SAME"].cross_band


def test_cross_band_hidden_when_one_band_missing(station):
    _module(station, "1", VHF)
    _rep("XB", "145.600", "431.050")
    assert repeaters_for_station(station) == []


def test_no_coords_sorts_by_callsign_no_500(station):
    _module(station, "1", VHF)
    _rep("ZZ", "145.600", "145.000", latitude=Decimal("48"), longitude=Decimal("14"))
    _rep("AA", "145.625", "145.025")
    rows = repeaters_for_station(station, radius_km=1)  # radius ignored
    assert [r.repeater.callsign for r in rows] == ["AA", "ZZ"]
    assert all(r.distance_km is None and r.bearing is None for r in rows)


def test_radius_filter(station):
    station.latitude, station.longitude = Decimal("48.0"), Decimal("14.0")
    station.save()
    _module(station, "1", VHF)
    _rep("NEAR", "145.625", "145.025", latitude=Decimal("48.0"), longitude=Decimal("14.1"))
    _rep("FAR", "145.600", "145.000", latitude=Decimal("48.0"), longitude=Decimal("15.0"))
    _rep("NOLOC", "145.650", "145.050")
    rows = repeaters_for_station(station, radius_km=20)
    assert [r.repeater.callsign for r in rows] == ["NEAR"]


def test_offline_module_ignored(station):
    _module(station, "1", VHF, online=False)
    _rep("TWO", "145.600", "145.000")
    assert station_bands(station) == {}
    assert repeaters_for_station(station) == []


def test_module_missing_required_cap_ignored(station):
    m = _module(station, "1", VHF)
    m.capability_descriptor = [c for c in m.capability_descriptor if c["name"] != "rx_tone"]
    m.save()
    assert station_bands(station) == {}


def test_search_q(station):
    _module(station, "1", VHF)
    _rep("OE5XLL", "145.600", "145.000", site_name="Linz Pfenningberg")
    _rep("OE1XAB", "145.625", "145.025", site_name="Wien")
    assert [r.repeater.callsign for r in repeaters_for_station(station, q="oe5x")] == ["OE5XLL"]
    assert [r.repeater.callsign for r in repeaters_for_station(station, q="wien")] == ["OE1XAB"]


def test_station_bands_and_band_of(station):
    _module(station, "1", VHF, module_id="fm")
    _module(station, "2", UHF, module_id="fm2")
    bands = station_bands(station)
    assert [(m.slot, m.module) for m in bands["vhf"]] == [("1", "fm")]
    assert bands["uhf"][0].band == "uhf"
    assert band_of(Decimal("145.6"), bands, station) == "vhf"
    assert band_of(Decimal("438.65"), bands, station) == "uhf"
    assert band_of(Decimal("300"), bands, station) is None


def test_haversine_and_bearing_known_pairs():
    assert haversine_km(48, 14, 48, 14) == 0
    assert 111 < haversine_km(48, 14, 49, 14) < 112
    assert bearing_label(48, 14, 49, 14) == "N"
    assert bearing_label(48, 14, 47, 14) == "S"
    assert bearing_label(48, 14, 48, 13) == "W"
    assert bearing_label(48, 14, 48.5, 14.6) == "NE"


def test_range_boundaries_inclusive(station):
    _module(station, "1", VHF)
    _rep("LO", "134.000", "134.000")
    _rep("HI", "174.000", "174.000")
    _rep("OUT", "174.00001", "145.000")
    assert [r.repeater.callsign for r in repeaters_for_station(station)] == ["HI", "LO"]


def test_malformed_descriptors_ignored(station):
    StationModule.objects.create(
        station=station, slot="1", module_id="bad", online=True, capability_descriptor="oops"
    )
    m = _module(station, "2", VHF, module_id="fm")
    m.capability_descriptor[0]["ranges"] = [{"name": "vhf"}, {"name": "x", "min": "a", "max": 1}]
    m.save()
    assert station_bands(station) == {}


def test_query_count_independent_of_repeater_count(station, django_assert_max_num_queries):
    _module(station, "1", VHF)
    for i in range(20):
        _rep(f"R{i}", "145.600", "145.000")
    with django_assert_max_num_queries(4):
        assert len(repeaters_for_station(station)) == 20


def test_bearing_wraps_near_north():
    assert bearing_label(48, 14, 49, 13.99) == "N"
    assert bearing_label(48, 14, 48.5, 14.6) == "NE"
    assert bearing_label(48, 14, 48.5, 13.4) == "NW"
