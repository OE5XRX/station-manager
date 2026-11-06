"""JSON endpoint ``stations/<pk>/control/repeaters/`` behind the repeater panel."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.control.models import StationModule
from apps.repeaters.models import Repeater
from apps.stations.models import Station

pytestmark = pytest.mark.django_db

VHF = {"name": "vhf", "min": 134.0, "max": 174.0}
UHF = {"name": "uhf", "min": 400.0, "max": 480.0}


def _desc(*ranges):
    return [
        {"name": "tx_frequency", "ranges": list(ranges)},
        {"name": "rx_frequency", "ranges": list(ranges)},
        {"name": "tx_tone"},
        {"name": "rx_tone"},
    ]


def _module(station, slot, *ranges, module_id="fm0", online=True):
    return StationModule.objects.create(
        station=station,
        slot=slot,
        module_id=module_id,
        online=online,
        capability_descriptor=_desc(*ranges),
    )


def _rep(callsign, out, inp, **kw):
    return Repeater.objects.create(
        callsign=callsign,
        output_frequency=Decimal(out),
        input_frequency=Decimal(inp),
        source="manual",
        **kw,
    )


@pytest.fixture
def viewer(db):
    # control.view is enough to read the list (applicant level).
    return User.objects.create_user(username="viewer", password="x")


def _get(client, station, **params):
    return client.get(reverse("control:station_repeaters", args=[station.pk]), params)


def test_anonymous_redirected(client):
    station = Station.objects.create(name="s-anon")
    r = _get(client, station)
    assert r.status_code in (301, 302)


def test_shape_sorted_by_distance(client, viewer):
    station = Station.objects.create(
        name="s-shape", latitude=Decimal("48.30"), longitude=Decimal("14.29")
    )
    _module(station, "1", VHF)
    _module(station, "2", VHF, UHF, module_id="fm1")
    far = _rep(
        "OE5FAR",
        "145.700",
        "145.100",
        latitude=Decimal("48.60"),
        longitude=Decimal("14.29"),
        bandwidth="12.5",
    )
    near = _rep(
        "OE5XLL",
        "145.600",
        "145.000",
        latitude=Decimal("48.35"),
        longitude=Decimal("14.39"),
        input_tone="77.0",
        site_name="Lichtenberg",
    )
    cross = _rep("OE5CB", "438.650", "145.050")
    client.force_login(viewer)
    r = _get(client, station)
    assert r.status_code == 200
    data = r.json()
    assert data["station_has_coords"] is True
    assert data["bands"] == {
        "vhf": [{"slot": "1", "module": "fm0"}, {"slot": "2", "module": "fm1"}],
        "uhf": [{"slot": "2", "module": "fm1"}],
    }
    assert data["attribution"]["url"] == "https://repeater.oevsv.at/"
    ids = [row["id"] for row in data["repeaters"]]
    assert ids[:2] == [near.pk, far.pk]
    assert cross.pk in ids
    first = data["repeaters"][0]
    assert first == {
        "id": near.pk,
        "callsign": "OE5XLL",
        "site_name": "Lichtenberg",
        "output_frequency": "145.60000",
        "input_frequency": "145.00000",
        "input_tone": "77.0",
        "output_tone": "none",
        "bandwidth": "",
        "distance_km": first["distance_km"],
        "bearing": "NE",
        "rx_band": "vhf",
        "tx_band": "vhf",
        "cross_band": False,
    }
    assert 5 < first["distance_km"] < 12
    row_cross = next(row for row in data["repeaters"] if row["id"] == cross.pk)
    assert row_cross["cross_band"] is True
    assert row_cross["distance_km"] is None and row_cross["bearing"] is None


def test_station_without_coords(client, viewer):
    station = Station.objects.create(name="s-nocoords")
    _module(station, "1", VHF)
    _rep("OE5ZZZ", "145.700", "145.100", latitude=Decimal("48.6"), longitude=Decimal("14.2"))
    _rep("OE5AAA", "145.600", "145.000")
    client.force_login(viewer)
    data = _get(client, station).json()
    assert data["station_has_coords"] is False
    assert [r["callsign"] for r in data["repeaters"]] == ["OE5AAA", "OE5ZZZ"]
    assert all(r["distance_km"] is None for r in data["repeaters"])


def test_search_filters(client, viewer):
    station = Station.objects.create(name="s-search")
    _module(station, "1", VHF)
    _rep("OE5XLL", "145.600", "145.000", site_name="Lichtenberg")
    _rep("OE5XBR", "145.650", "145.050", site_name="Breitenstein")
    client.force_login(viewer)
    data = _get(client, station, q="licht").json()
    assert [r["callsign"] for r in data["repeaters"]] == ["OE5XLL"]
    # Over-long query is truncated, never a 500.
    assert _get(client, station, q="x" * 5000).status_code == 200


def test_no_online_modules(client, viewer):
    station = Station.objects.create(name="s-empty")
    _module(station, "1", VHF, online=False)
    _rep("OE5XLL", "145.600", "145.000")
    client.force_login(viewer)
    data = _get(client, station).json()
    assert data["bands"] == {} and data["repeaters"] == []


def test_unknown_station_404(client, viewer):
    client.force_login(viewer)
    r = client.get(reverse("control:station_repeaters", args=[999999]))
    assert r.status_code == 404
