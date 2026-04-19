from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable

from .models import current_sequence

UNASSIGNED_KEY = "__unassigned__"


def group_stations_by_sequence(stations: Iterable) -> OrderedDict[str, list]:
    """Bucket each station by the first sequence tag it carries.

    Returns an OrderedDict keyed by `StationTag.slug` in sequence
    position order (plus UNASSIGNED_KEY at the tail), each value being
    the list of Stations that land in that bucket. Slug is used instead
    of name because it's URL-safe and stable against tag renames — the
    dashboard form's action URL depends on this key round-tripping
    cleanly through a path segment.
    """
    seq = current_sequence()
    ordered_tag_slugs = list(seq.entries.select_related("tag").values_list("tag__slug", flat=True))

    buckets: OrderedDict[str, list] = OrderedDict()
    for slug in ordered_tag_slugs:
        buckets[slug] = []
    buckets[UNASSIGNED_KEY] = []

    for station in stations:
        # Use .all() so a caller's .prefetch_related("tags") actually hits.
        # values_list() bypasses the prefetch cache and re-queries per row.
        station_tag_slugs = {t.slug for t in station.tags.all()}
        placed = False
        for slug in ordered_tag_slugs:
            if slug in station_tag_slugs:
                buckets[slug].append(station)
                placed = True
                break
        if not placed:
            buckets[UNASSIGNED_KEY].append(station)

    return buckets
