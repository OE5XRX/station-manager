"""Record accepted heartbeat metric values as ``MetricSample`` rows (spec §2).

Called from ``apps.monitoring.metrics_ingest.ingest_metrics`` after the current values
are saved. History is best-effort: any failure (e.g. a missing monthly partition during
a rollout gap) is logged and swallowed so the heartbeat is never affected.
"""

import logging
import math
from datetime import datetime

from django.core.cache import cache
from django.db import transaction

from apps.history.models import MetricSample

logger = logging.getLogger(__name__)

INGEST_ERROR_CACHE_KEY = "history:ingest-error"
INGEST_ERROR_TTL = 3600

# ``normalize_value`` accepts any finite float, but history aggregates them: two samples near
# 1e308 in one hour overflow Postgres ``AVG``/``SUM`` ("value out of range") and abort the
# whole compaction chunk — for every station, the watermark would never advance — and the
# 5m/1d API aggregates would 500 (SQLite silently stores inf). No physical metric comes
# anywhere near 1e15, so values beyond it are skipped here (not recorded) at the boundary.
HISTORY_MAX_ABS = 1e15


def _recordable(value: dict) -> bool:
    """True if min/max/avg are all finite and within ``±HISTORY_MAX_ABS``."""
    try:
        parts = (float(value["min"]), float(value["max"]), float(value["avg"]))
    except (KeyError, TypeError, ValueError):
        return False
    return all(math.isfinite(x) and abs(x) <= HISTORY_MAX_ABS for x in parts)


def record_samples(station, ts: datetime, values: dict[str, dict]) -> int:
    """Insert one sample per ``{key: {"min", "max", "avg"}}`` at ``ts``; return rows sent.

    ``ignore_conflicts``: a duplicate heartbeat with the same ``ts`` is not an error.
    Values outside ``±HISTORY_MAX_ABS`` or non-finite are skipped (see there); the other
    keys of the heartbeat are still recorded. Never raises.
    """
    rows = [
        MetricSample(
            station=station,
            metric_key=key,
            ts=ts,
            min=value["min"],
            max=value["max"],
            avg=value["avg"],
        )
        for key, value in (values or {}).items()
        if _recordable(value)
    ]
    if not rows:
        return 0
    try:
        # Savepoint: a failed INSERT must not poison an enclosing transaction
        # (on Postgres every later query would fail with "transaction aborted").
        with transaction.atomic():
            MetricSample.objects.bulk_create(rows, ignore_conflicts=True)
    except Exception:
        # cache.add is atomic and only succeeds when absent -> one error log per hour.
        if cache.add(INGEST_ERROR_CACHE_KEY, 1, INGEST_ERROR_TTL):
            logger.exception(
                "history: recording %d sample(s) for station %s failed", len(rows), station.pk
            )
        else:
            logger.debug("history: recording samples for station %s failed", station.pk)
        return 0
    return len(rows)
