"""Station-side repeater selection: band filtering, distance, cross-band flag.

Pure Python (no PostGIS). A repeater is usable by a station when both its
output (our RX) and input (our TX) frequency fall into a band advertised by one
of the station's online modules. If the two frequencies sit in different bands
the row is flagged ``cross_band`` (needs two modules; not yet applicable).
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple

from django.db.models import Q

from .models import Repeater

REQUIRED_CAPS = ("rx_frequency", "tx_frequency", "tx_tone", "rx_tone")
_POINTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_EARTH_RADIUS_KM = 6371.0088


class ModuleRef(NamedTuple):
    slot: str
    module: str
    band: str


@dataclass
class RepeaterRow:
    repeater: Repeater
    distance_km: float | None
    bearing: str | None
    rx_band: str
    tx_band: str
    cross_band: bool


def _caps(module) -> dict[str, dict]:
    descriptor = module.capability_descriptor
    if not isinstance(descriptor, list):
        return {}
    return {
        c["name"]: c for c in descriptor if isinstance(c, dict) and isinstance(c.get("name"), str)
    }


def _module_ranges(module) -> list[tuple[str, Decimal, Decimal]]:
    """``(band name, min, max)`` from the module's ``rx_frequency`` capability."""
    ranges = _caps(module).get("rx_frequency", {}).get("ranges")
    if not isinstance(ranges, list):
        return []
    out = []
    for rng in ranges:
        try:
            name = str(rng["name"])
            lo, hi = Decimal(str(rng["min"])), Decimal(str(rng["max"]))
        except (KeyError, ArithmeticError, TypeError, ValueError):
            continue
        if lo.is_finite() and hi.is_finite():
            out.append((name, lo, hi))
    return out


def _online_modules(station):
    for module in station.modules.filter(online=True).order_by("slot", "module_id"):
        caps = _caps(module)
        if all(c in caps for c in REQUIRED_CAPS):
            yield module


def usable_modules(station):
    """Online modules advertising every capability a repeater preset needs."""
    return list(_online_modules(station))


def has_capability(module, name: str) -> bool:
    return name in _caps(module)


def module_band_of(module, freq: Decimal) -> str | None:
    """Band of ``module`` (its ``rx_frequency`` ranges) containing ``freq``, else None."""
    ranges: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for name, lo, hi in _module_ranges(module):
        ranges.setdefault(name, []).append((lo, hi))
    return _match_band(freq, ranges)


def station_bands(station) -> dict[str, list[ModuleRef]]:
    bands: dict[str, list[ModuleRef]] = {}
    for module in _online_modules(station):
        for name, _lo, _hi in _module_ranges(module):
            bands.setdefault(name, []).append(ModuleRef(module.slot, module.module_id, name))
    return bands


def _band_ranges(station, bands) -> dict[str, list[tuple[Decimal, Decimal]]]:
    """``{band: [(min, max), ...]}`` for ``bands`` (one pass over the modules)."""
    out: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for module in _online_modules(station):
        for name, lo, hi in _module_ranges(module):
            if name in bands:
                out.setdefault(name, []).append((lo, hi))
    return out


def _match_band(freq: Decimal, ranges: dict[str, list[tuple[Decimal, Decimal]]]) -> str | None:
    freq = Decimal(str(freq))
    for name, spans in ranges.items():
        if any(lo <= freq <= hi for lo, hi in spans):
            return name
    return None


def band_of(freq: Decimal, bands: dict[str, list[ModuleRef]], station) -> str | None:
    """Band name containing ``freq`` among the station's ``bands``, else None.

    Only ``rx_frequency`` ranges are consulted: the FM module advertises the same
    named band for ``tx_frequency``, so a frequency is usable for both directions.
    """
    return _match_band(freq, _band_ranges(station, bands))


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = p2 - p1
    dlmb = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_label(lat1, lon1, lat2, lon2) -> str:
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dlmb = math.radians(float(lon2) - float(lon1))
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _POINTS[int((deg + 22.5) // 45) % 8]


def repeaters_for_station(
    station, q: str = "", radius_km: float | None = None
) -> list[RepeaterRow]:
    bands = station_bands(station)
    if not bands:
        return []
    ranges = _band_ranges(station, bands)
    qs = Repeater.objects.visible()
    if q:
        qs = qs.filter(Q(callsign__icontains=q) | Q(site_name__icontains=q))

    has_coords = station.latitude is not None and station.longitude is not None
    rows: list[RepeaterRow] = []
    for rep in qs:
        rx_band = _match_band(rep.output_frequency, ranges)
        tx_band = _match_band(rep.input_frequency, ranges)
        if rx_band is None or tx_band is None:
            continue
        distance = bearing = None
        if has_coords and rep.latitude is not None and rep.longitude is not None:
            distance = haversine_km(
                station.latitude, station.longitude, rep.latitude, rep.longitude
            )
            bearing = bearing_label(
                station.latitude, station.longitude, rep.latitude, rep.longitude
            )
        if has_coords and radius_km is not None and (distance is None or distance > radius_km):
            continue
        rows.append(RepeaterRow(rep, distance, bearing, rx_band, tx_band, rx_band != tx_band))

    if has_coords:
        rows.sort(key=lambda r: (r.distance_km is None, r.distance_km or 0.0, r.repeater.callsign))
    else:
        rows.sort(key=lambda r: r.repeater.callsign)
    return rows
