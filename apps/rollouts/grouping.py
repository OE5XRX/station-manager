from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable

from apps.stations.models import RolloutGroup

from .models import current_sequence

UNASSIGNED_KEY = "__unassigned__"


def ordered_groups() -> list[RolloutGroup]:
    """Every RolloutGroup in dashboard order.

    Sequence groups first (by position), then the groups without a sequence
    position (alphabetical by name).
    """
    seq = current_sequence()
    in_sequence = [e.group for e in seq.entries.select_related("group").order_by("position")]
    seen = {g.pk for g in in_sequence}
    rest = [g for g in RolloutGroup.objects.order_by("name") if g.pk not in seen]
    return in_sequence + rest


def group_stations_by_sequence(
    stations: Iterable, groups: list[RolloutGroup] | None = None
) -> OrderedDict[str, list]:
    """Bucket each station by its rollout group.

    Returns an OrderedDict keyed by `RolloutGroup.slug`: sequence groups in
    position order, then the remaining groups by name, then UNASSIGNED_KEY
    (stations without a group). Slug is used instead of name because it's
    URL-safe and stable against renames — the dashboard form's action URL
    depends on this key round-tripping cleanly through a path segment.

    Reads ``station.rollout_group_id`` only, so callers don't need a join; pass
    ``groups`` (from ``ordered_groups()``) to avoid re-querying them.
    """
    if groups is None:
        groups = ordered_groups()
    slug_by_pk = {g.pk: g.slug for g in groups}

    buckets: OrderedDict[str, list] = OrderedDict((g.slug, []) for g in groups)
    buckets[UNASSIGNED_KEY] = []
    for station in stations:
        buckets[slug_by_pk.get(station.rollout_group_id, UNASSIGNED_KEY)].append(station)
    return buckets
