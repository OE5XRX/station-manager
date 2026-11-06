"""Build the effective scan list for a station (own lists + radius repeaters).

The server owns the list, the agent owns the loop: this module only resolves
sources into concrete entries (``slot``/``module`` + frequencies/tones) in the
shape of the ``scan_start`` / ``scan_update`` agent frames.
"""

from decimal import Decimal

from .models import Repeater, ScanList
from .selection import ModuleRef, _band_ranges, _match_band, repeaters_for_station, station_bands


class NoCoordinates(Exception):  # noqa: N818 - name fixed by the plan (Task 9b imports it)
    """A radius was requested but the station has no coordinates."""


def _resolve_module(
    band: str, bands: dict[str, list[ModuleRef]], band_map: dict[str, str]
) -> ModuleRef | None:
    candidates = bands.get(band) or []
    wanted = band_map.get(band)
    if wanted is not None:
        for ref in candidates:
            if ref.slot == str(wanted):
                return ref
    # No (valid) mapping: the single module of the band; with several, the first by slot.
    return candidates[0] if candidates else None


def _entry(ref, label, module: ModuleRef, rx, tx, rx_tone, tx_tone) -> dict:
    return {
        "ref": ref,
        "label": label,
        "slot": module.slot,
        "module": module.module,
        "rx_frequency": float(rx),
        "tx_frequency": float(tx),
        "rx_tone": rx_tone,
        "tx_tone": tx_tone,
    }


def build_scan_entries(
    station,
    user,
    *,
    radius_km: float | None,
    list_ids: list[int],
    band_map: dict[str, str],
) -> list[dict]:
    has_coords = station.latitude is not None and station.longitude is not None
    if radius_km is not None and not has_coords:
        raise NoCoordinates

    bands = station_bands(station)
    if not bands:
        return []
    ranges = _band_ranges(station, bands)
    band_map = band_map or {}

    entries: list[dict] = []
    seen: set[tuple[Decimal, str]] = set()

    def add(ref, label, rx, tx, rx_tone, tx_tone):
        rx_band = _match_band(rx, ranges)
        tx_band = _match_band(tx, ranges)
        if rx_band is None or rx_band != tx_band:  # outside our bands, or cross-band
            return
        key = (Decimal(str(rx)), rx_tone)
        if key in seen:
            return
        module = _resolve_module(rx_band, bands, band_map)
        if module is None:
            return
        seen.add(key)
        entries.append(_entry(ref, label, module, rx, tx, rx_tone, tx_tone))

    # Own lists, in the requested order; foreign / unknown ids are ignored.
    owned = {
        sl.pk: sl
        for sl in ScanList.objects.filter(owner=user, pk__in=list_ids).prefetch_related(
            "entries__repeater"
        )
    }
    visible_ids = set(Repeater.objects.visible().values_list("pk", flat=True))
    for list_id in list_ids:
        scan_list = owned.get(list_id)
        if scan_list is None:
            continue
        for item in scan_list.entries.all():
            if item.repeater_id is not None:
                rep = item.repeater
                if rep.pk not in visible_ids:
                    continue
                add(
                    f"r:{rep.pk}",
                    item.label or rep.callsign,
                    rep.output_frequency,
                    rep.input_frequency,
                    rep.output_tone,
                    rep.input_tone,
                )
            else:
                add(
                    f"f:{item.pk}",
                    item.label or f"{item.frequency.normalize():f} MHz",
                    item.frequency,
                    item.frequency,
                    "none",
                    item.tone,
                )

    if radius_km is not None:
        for row in repeaters_for_station(station, radius_km=radius_km):
            rep = row.repeater
            add(
                f"r:{rep.pk}",
                rep.callsign,
                rep.output_frequency,
                rep.input_frequency,
                rep.output_tone,
                rep.input_tone,
            )
    return entries
