"""Ingest the heartbeat ``metrics`` / ``metric_descriptors`` blocks (spec §2, §3).

Hot path (every station, every heartbeat): DB access is bounded and independent
of the number of keys — one query loads the definitions for all incoming keys,
descriptors are upserted via bulk_create/bulk_update, ``last_seen`` is bumped
with a single UPDATE, and the telemetry row is written once.
"""

import logging

from django.core.cache import cache
from django.utils import timezone

from apps.monitoring.models import MAX_DEFINITIONS, MetricDefinition
from apps.stations.models import StationTelemetry
from station_agent.metrics_contract import (
    MAX_KEYS,
    clean_descriptor,
    is_valid_key,
    mount_to_instance,
    normalize_value,
)
from station_agent.metrics_contract import (
    descriptors_hash as compute_descriptors_hash,
)

logger = logging.getLogger(__name__)

CAP_WARNING_TTL = 3600
_AGENT_FIELDS = [
    "label",
    "unit",
    "group",
    "precision",
    "min_value",
    "max_value",
    "source_agent_version",
    "last_seen",
]
_SOURCE_VERSION_LEN = MetricDefinition._meta.get_field("source_agent_version").max_length


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clean_descriptors(descriptors: list) -> list[dict]:
    """clean_descriptor each, drop invalid, dedupe by key (last wins), cap at MAX_KEYS."""
    by_key: dict[str, dict] = {}
    for raw in descriptors:
        d = clean_descriptor(raw)
        if d is None:
            continue
        by_key.pop(d["key"], None)  # re-insert so "last wins" also decides the cap order
        by_key[d["key"]] = d
    cleaned = list(by_key.values())
    if len(cleaned) > MAX_KEYS:
        logger.warning(
            "metric descriptors: dropped %d over the %d cap", len(cleaned) - MAX_KEYS, MAX_KEYS
        )
        cleaned = cleaned[-MAX_KEYS:]
    return cleaned


def _collect_values(metrics) -> dict[str, dict]:
    """Validate + normalise the agent ``metrics`` block (first MAX_KEYS items)."""
    if not isinstance(metrics, dict):
        if metrics is not None:
            logger.warning("Ignoring non-dict metrics block (%s)", type(metrics).__name__)
        return {}
    if len(metrics) > MAX_KEYS:
        logger.warning(
            "metrics: dropped %d keys over the %d cap", len(metrics) - MAX_KEYS, MAX_KEYS
        )
    values: dict[str, dict] = {}
    invalid = 0
    for i, (key, raw) in enumerate(metrics.items()):
        if i >= MAX_KEYS:
            break
        value = normalize_value(raw) if is_valid_key(key) else None
        if value is None:
            invalid += 1
            continue
        values[key] = value
    if invalid:
        logger.info("metrics: dropped %d invalid key(s)/value(s)", invalid)
    return values


def _legacy_values(inventory, telemetry) -> dict[str, dict]:
    """Derive legacy metrics from an old agent's inventory (spec §3.3)."""
    inv = _as_dict(inventory)
    candidates: list[tuple[str, object]] = [
        ("cpu.temp_c", _as_dict(inv.get("cpu")).get("temperature_c")),
        ("mem.used_pct", _as_dict(inv.get("ram")).get("usage_percent")),
        ("sys.uptime_s", _as_dict(_as_dict(telemetry).get("boot")).get("uptime_seconds")),
    ]
    disks = inv.get("disk")
    if isinstance(disks, list):
        for disk in disks[:MAX_KEYS]:
            disk = _as_dict(disk)
            mount = disk.get("mount")
            if isinstance(mount, str) and mount:
                candidates.append(
                    (f"disk.{mount_to_instance(mount)}.used_pct", disk.get("usage_percent"))
                )
    out: dict[str, dict] = {}
    for key, raw in candidates:
        if not _is_number(raw) or not is_valid_key(key):
            continue
        value = normalize_value(raw)
        if value is not None:
            out[key] = value
    return out


def _out_of_range(value: dict, lo, hi) -> bool:
    return any(
        (lo is not None and value[f] < lo) or (hi is not None and value[f] > hi)
        for f in ("min", "max", "avg")
    )


def _warn_cap(key: str) -> None:
    # cache.add is atomic and only succeeds when the key is absent -> once per hour.
    if cache.add(f"metrics:cap:{key}", 1, CAP_WARNING_TTL):
        logger.warning(
            "MetricDefinition cap (%d) reached: key %r not registered, values dropped",
            MAX_DEFINITIONS,
            key,
        )


def _upsert_descriptors(cleaned, defs, *, agent_version, now, count_fn) -> None:
    """Update existing definitions (agent fields only) and create new ones under the cap.

    ``defs`` (key -> MetricDefinition) is updated in place with created rows.
    """
    version = (agent_version or "")[:_SOURCE_VERSION_LEN]
    to_update, to_create = [], []
    for d in cleaned:
        obj = defs.get(d["key"])
        if obj is None:
            obj = MetricDefinition(key=d["key"], last_seen=now)
            to_create.append(obj)
        else:
            to_update.append(obj)
        obj.label = d["label"]
        obj.unit = d["unit"]
        obj.group = d["group"]
        obj.precision = d["precision"]
        obj.min_value = d["min"]
        obj.max_value = d["max"]
        obj.source_agent_version = version
        obj.last_seen = now
    if to_update:
        # Only agent fields: operator overrides / hidden / blocked are never touched.
        MetricDefinition.objects.bulk_update(to_update, _AGENT_FIELDS)
    if to_create:
        room = max(0, MAX_DEFINITIONS - count_fn())
        for obj in to_create[room:]:
            _warn_cap(obj.key)
        allowed = to_create[:room]
        if allowed:
            # ignore_conflicts: a concurrent heartbeat may create the same key.
            MetricDefinition.objects.bulk_create(allowed, ignore_conflicts=True)
            for obj in allowed:
                defs[obj.key] = obj


def ingest_metrics(
    station,
    *,
    metrics,
    descriptors,
    descriptors_hash,
    agent_version,
    inventory,
    telemetry: dict | None = None,
) -> bool:
    """Store the heartbeat metrics; return True if the agent must resend its descriptors."""
    now = timezone.now()
    list_sent = isinstance(descriptors, list)
    cleaned = _clean_descriptors(descriptors) if list_sent else []

    values = _collect_values(metrics)
    for key, value in _legacy_values(inventory, telemetry).items():
        # MAX_KEYS bounds the merged set: an inventory with many disks must not
        # bypass the per-heartbeat cap via the legacy fallback.
        if len(values) >= MAX_KEYS:
            break
        values.setdefault(key, value)

    lookup_keys = set(values) | {d["key"] for d in cleaned}
    defs = (
        {d.key: d for d in MetricDefinition.objects.filter(key__in=lookup_keys)}
        if lookup_keys
        else {}
    )

    _count = None

    def count_fn():
        nonlocal _count
        if _count is None:
            _count = MetricDefinition.objects.count()
        return _count

    descriptor_keys = set()
    if cleaned:
        _upsert_descriptors(cleaned, defs, agent_version=agent_version, now=now, count_fn=count_fn)
        descriptor_keys = {d["key"] for d in cleaned}
        _count = None  # rows may have been created

    stored: dict[str, dict] = {}
    seen_pks, rejected = [], []
    has_undefined = False
    ts = now.isoformat()
    for key, value in values.items():
        definition = defs.get(key)
        if definition is None:
            if count_fn() >= MAX_DEFINITIONS:
                _warn_cap(key)
                continue
            has_undefined = True
        else:
            if definition.is_blocked:
                continue
            if _out_of_range(value, definition.range_min, definition.range_max):
                logger.info(
                    "metrics: %s=%s outside plausible range for station %s", key, value, station.pk
                )
                definition.last_rejected_at = now
                definition.last_rejected_value = value
                rejected.append(definition)
                continue
            if key not in descriptor_keys and definition.pk is not None:
                seen_pks.append(definition.pk)
        stored[key] = {**value, "ts": ts}

    if seen_pks:
        MetricDefinition.objects.filter(pk__in=seen_pks).update(last_seen=now)
    if rejected:
        with_pk = [d for d in rejected if d.pk is not None]
        if with_pk:
            MetricDefinition.objects.bulk_update(
                with_pk, ["last_rejected_at", "last_rejected_value"]
            )
        # Rows created in this heartbeat via ignore_conflicts carry no pk; rare path.
        for d in rejected:
            if d.pk is None:
                MetricDefinition.objects.filter(key=d.key).update(
                    last_rejected_at=d.last_rejected_at, last_rejected_value=d.last_rejected_value
                )

    new_hash = compute_descriptors_hash(cleaned) if list_sent else None
    tel = StationTelemetry.objects.filter(station=station).first()
    if tel is None and (stored or new_hash is not None):
        tel = StationTelemetry(station=station)
    if tel is not None:
        tel.metrics = stored
        if new_hash is not None:
            tel.metric_descriptors_hash = new_hash
        if tel.pk is None:
            tel.save()
        else:
            tel.save(update_fields=["metrics", "metric_descriptors_hash"])

    if list_sent:
        return False
    stored_hash = tel.metric_descriptors_hash if tel is not None else ""
    if descriptors_hash and descriptors_hash != stored_hash:
        return True
    return has_undefined
