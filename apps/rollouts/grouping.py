from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable

from .models import current_sequence

UNASSIGNED_KEY = "__unassigned__"


def group_stations_by_sequence(stations: Iterable) -> OrderedDict[str, list]:
    """Bucket each station by the first sequence tag it carries.

    Returns an OrderedDict with one key per sequence entry (in position
    order), each mapping to a list of Stations, plus an UNASSIGNED_KEY
    bucket at the end for stations with no matching tag.
    """
    seq = current_sequence()
    ordered_tag_names = list(seq.entries.select_related("tag").values_list("tag__name", flat=True))

    buckets: OrderedDict[str, list] = OrderedDict()
    for name in ordered_tag_names:
        buckets[name] = []
    buckets[UNASSIGNED_KEY] = []

    for station in stations:
        station_tag_names = set(station.tags.values_list("name", flat=True))
        placed = False
        for name in ordered_tag_names:
            if name in station_tag_names:
                buckets[name].append(station)
                placed = True
                break
        if not placed:
            buckets[UNASSIGNED_KEY].append(station)

    return buckets
