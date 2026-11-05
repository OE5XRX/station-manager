"""Record accepted heartbeat metric values as ``MetricSample`` rows (spec §2).

Called from ``apps.monitoring.metrics_ingest.ingest_metrics`` after the current values
are saved. History is best-effort: any failure (e.g. a missing monthly partition during
a rollout gap) is logged and swallowed so the heartbeat is never affected.
"""

import logging
from datetime import datetime

from django.core.cache import cache
from django.db import transaction

from apps.history.models import MetricSample

logger = logging.getLogger(__name__)

INGEST_ERROR_CACHE_KEY = "history:ingest-error"
INGEST_ERROR_TTL = 3600


def record_samples(station, ts: datetime, values: dict[str, dict]) -> int:
    """Insert one sample per ``{key: {"min", "max", "avg"}}`` at ``ts``; return rows sent.

    ``ignore_conflicts``: a duplicate heartbeat with the same ``ts`` is not an error.
    Never raises.
    """
    if not values:
        return 0
    rows = [
        MetricSample(
            station=station,
            metric_key=key,
            ts=ts,
            min=value["min"],
            max=value["max"],
            avg=value["avg"],
        )
        for key, value in values.items()
    ]
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
