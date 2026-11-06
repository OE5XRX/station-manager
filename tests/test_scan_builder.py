"""build_scan_entries: order, dedupe, band skip, band_map resolution, NoCoordinates."""

from decimal import Decimal

import pytest

from apps.control.models import StationModule
from apps.repeaters.models import Repeater, ScanList, ScanListEntry
from apps.repeaters.scan import NoCoordinates, build_scan_entries

pytestmark = pytest.mark.django_db

VHF = ("vhf", 134, 174)
UHF = ("uhf", 400, 480)


def _module(station, slot, band, module_id="fm"):
    name, lo, hi = band
    rng = [{"name": name, "min": lo, "max": hi}]
    return StationModule.objects.create(
        station=station,
        slot=slot,
        module_id=module_id,
        online=True,
        capability_descriptor=[
            {"name": "rx_frequency", "ranges": rng},
            {"name": "tx_frequency", "ranges": rng},
            {"name": "tx_tone"},
            {"name": "rx_tone"},
        ],
    )


def _rep(callsign, out, inp, **kw):
    kw.setdefault("source", "manual")
    return Repeater.objects.create(
        callsign=callsign, output_frequency=Decimal(out), input_frequency=Decimal(inp), **kw
    )


def _list(user, name="L"):
    return ScanList.objects.create(owner=user, name=name)


def _add(scan_list, position, **kw):
    return ScanListEntry.objects.create(scan_list=scan_list, position=position, **kw)


def _build(station, user, **kw):
    kw.setdefault("radius_km", None)
    kw.setdefault("list_ids", [])
    kw.setdefault("band_map", {})
    return build_scan_entries(station, user, **kw)


def test_entry_shape_for_repeater(station, member_user):
    _module(station, "1", VHF)
    rep = _rep("OE5XLL", "145.600", "145.000", output_tone="none", input_tone="88.5")
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    (entry,) = _build(station, member_user, list_ids=[sl.pk])
    assert entry == {
        "ref": f"r:{rep.pk}",
        "label": "OE5XLL",
        "slot": "1",
        "module": "fm",
        "rx_frequency": 145.6,
        "tx_frequency": 145.0,
        "rx_tone": "none",
        "tx_tone": "88.5",
    }


def test_free_frequency_is_simplex_with_tx_tone(station, member_user):
    _module(station, "1", VHF)
    sl = _list(member_user)
    item = _add(sl, 0, frequency=Decimal("145.50000"), tone="77.0", label="Calling")
    (entry,) = _build(station, member_user, list_ids=[sl.pk])
    assert entry["ref"] == f"f:{item.pk}"
    assert entry["label"] == "Calling"
    assert entry["rx_frequency"] == entry["tx_frequency"] == 145.5
    assert (entry["rx_tone"], entry["tx_tone"]) == ("none", "77.0")


def test_order_lists_in_given_order_then_radius_by_distance(station, member_user):
    station.latitude, station.longitude = Decimal("48.0"), Decimal("14.0")
    station.save()
    _module(station, "1", VHF)
    _rep("NEAR", "145.600", "145.000", latitude=Decimal("48.1"), longitude=Decimal("14.0"))
    _rep("FAR", "145.700", "145.100", latitude=Decimal("48.5"), longitude=Decimal("14.0"))
    a, b = _rep("AAA", "145.650", "145.050"), _rep("BBB", "145.675", "145.075")
    l1, l2 = _list(member_user, "one"), _list(member_user, "two")
    _add(l1, 0, repeater=a)
    _add(l2, 0, repeater=b)
    entries = _build(station, member_user, list_ids=[l2.pk, l1.pk], radius_km=100)
    assert [e["label"] for e in entries] == ["BBB", "AAA", "NEAR", "FAR"]


def test_dedupe_on_rx_frequency_and_tone(station, member_user):
    _module(station, "1", VHF)
    rep = _rep("OE5XLL", "145.600", "145.000")
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    _add(sl, 1, frequency=Decimal("145.6"), tone="none")  # same rx freq + rx tone
    _add(sl, 2, repeater=rep)
    assert len(_build(station, member_user, list_ids=[sl.pk])) == 1


def test_dedupe_keeps_different_rx_tone(station, member_user):
    _module(station, "1", VHF)
    a = _rep("A", "145.600", "145.000", output_tone="none")
    b = _rep("B", "145.600", "145.000", output_tone="88.5")
    sl = _list(member_user)
    _add(sl, 0, repeater=a)
    _add(sl, 1, repeater=b)
    assert len(_build(station, member_user, list_ids=[sl.pk])) == 2


def test_dedupe_between_list_and_radius(station, member_user):
    station.latitude, station.longitude = Decimal("48.0"), Decimal("14.0")
    station.save()
    _module(station, "1", VHF)
    rep = _rep("OE5XLL", "145.600", "145.000", latitude=Decimal("48.1"), longitude=Decimal("14.0"))
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    assert len(_build(station, member_user, list_ids=[sl.pk], radius_km=50)) == 1


def test_entries_outside_station_bands_skipped(station, member_user):
    _module(station, "1", VHF)
    uhf = _rep("SEVENTY", "438.650", "431.050")
    vhf = _rep("TWO", "145.600", "145.000")
    sl = _list(member_user)
    _add(sl, 0, repeater=uhf)
    _add(sl, 1, repeater=vhf)
    _add(sl, 2, frequency=Decimal("433.5"))
    assert [e["label"] for e in _build(station, member_user, list_ids=[sl.pk])] == ["TWO"]


def test_cross_band_skipped(station, member_user):
    _module(station, "1", VHF)
    _module(station, "2", UHF)
    cross = _rep("CROSS", "145.600", "431.050")
    sl = _list(member_user)
    _add(sl, 0, repeater=cross)
    assert _build(station, member_user, list_ids=[sl.pk]) == []


def test_band_map_resolution(station, member_user):
    _module(station, "1", VHF)
    _module(station, "2", VHF, module_id="fm2")
    rep = _rep("OE5XLL", "145.600", "145.000")
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    (entry,) = _build(station, member_user, list_ids=[sl.pk], band_map={"vhf": "2"})
    assert (entry["slot"], entry["module"]) == ("2", "fm2")


def test_single_module_used_without_band_map(station, member_user):
    _module(station, "3", UHF)
    rep = _rep("SEVENTY", "438.650", "431.050")
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    (entry,) = _build(station, member_user, list_ids=[sl.pk])
    assert entry["slot"] == "3"


def test_unknown_band_map_slot_falls_back_to_single_module(station, member_user):
    _module(station, "1", VHF)
    rep = _rep("OE5XLL", "145.600", "145.000")
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    (entry,) = _build(station, member_user, list_ids=[sl.pk], band_map={"vhf": "9"})
    assert entry["slot"] == "1"


def test_radius_without_coordinates_raises(station, member_user):
    _module(station, "1", VHF)
    with pytest.raises(NoCoordinates):
        _build(station, member_user, radius_km=50)


def test_lists_without_coordinates_still_work(station, member_user):
    _module(station, "1", VHF)
    rep = _rep("OE5XLL", "145.600", "145.000")
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    assert len(_build(station, member_user, list_ids=[sl.pk])) == 1


def test_foreign_lists_ignored(station, member_user, staff_user):
    _module(station, "1", VHF)
    rep = _rep("OE5XLL", "145.600", "145.000")
    theirs = _list(staff_user)
    _add(theirs, 0, repeater=rep)
    assert _build(station, member_user, list_ids=[theirs.pk]) == []


def test_inactive_repeater_skipped(station, member_user):
    _module(station, "1", VHF)
    rep = _rep("GONE", "145.600", "145.000", active=False)
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    assert _build(station, member_user, list_ids=[sl.pk]) == []


def test_station_without_modules_gives_empty_list(station, member_user):
    rep = _rep("OE5XLL", "145.600", "145.000")
    sl = _list(member_user)
    _add(sl, 0, repeater=rep)
    assert _build(station, member_user, list_ids=[sl.pk]) == []
