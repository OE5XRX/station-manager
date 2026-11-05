"""History upkeep: hourly compaction, partition creation, retention.

Run by ``manage.py history_maintain [--loop]``. Order matters (``run_maintenance``):
compact first, then create partitions, then drop — a raw partition is only dropped once all
of its hours are compacted (its end ``<= HistoryState.compacted_until``).

All hour/month boundaries are UTC (project ``TIME_ZONE`` is Europe/Vienna — never truncate
in local time). On PostgreSQL retention drops whole monthly partitions; on other backends
(SQLite dev/tests) the equivalent row delete with the same boundaries and guard is used.
"""

import logging
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Avg, Count, Max, Min
from django.db.models.functions import TruncHour
from django.utils import timezone

from apps.history import partitions
from apps.history.models import HistoryState, MetricHourly, MetricSample

logger = logging.getLogger(__name__)

RAW_TABLE = MetricSample._meta.db_table
HOURLY_TABLE = MetricHourly._meta.db_table

# Compaction stops at ``hour_floor(now - COMPACT_GRACE)``, not at the current hour. Ingest
# stamps ``ts = now`` *before* its INSERT commits; a sample stamped 10:59:59 may become
# visible only after 11:00. Without a grace, a compaction in that gap would miss the row
# and move the watermark past hour 10 for good (the hour is never recomputed). The grace
# must exceed the longest heartbeat transaction by far — 5 min is generous.
COMPACT_GRACE = timedelta(minutes=5)

# Partition CREATE/DROP takes ACCESS EXCLUSIVE on the partitioned parent; a DDL stuck behind
# a long reader would queue every heartbeat INSERT behind itself. With this transaction-local
# ``lock_timeout`` the DDL fails fast instead (logged by ``run_maintenance``, retried next loop).
DDL_LOCK_TIMEOUT = "5s"


def _utc(now: datetime | None) -> datetime:
    now = timezone.now() if now is None else now
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)


def hour_floor(dt: datetime) -> datetime:
    """UTC start of the hour containing ``dt``."""
    return _utc(dt).replace(minute=0, second=0, microsecond=0)


def _set_ddl_lock_timeout() -> None:
    """``SET LOCAL lock_timeout = DDL_LOCK_TIMEOUT`` (PostgreSQL only; needs a transaction)."""
    if partitions.is_partitioned(connection):
        with connection.cursor() as cur:
            # set_config(..., is_local=true) == SET LOCAL, but parameterised.
            cur.execute("SELECT set_config('lock_timeout', %s, true)", [DDL_LOCK_TIMEOUT])


# --- compaction --------------------------------------------------------------------------


def _compact_chunk(start: datetime, end: datetime) -> int:
    """Upsert ``MetricHourly`` for every hour in ``[start, end)``; returns rows upserted."""
    rows = (
        MetricSample.objects.filter(ts__gte=start, ts__lt=end)
        .annotate(hour=TruncHour("ts", tzinfo=UTC))
        .values("station_id", "metric_key", "hour")
        .annotate(
            mn=Min("min"),
            mx=Max("max"),
            av=Avg("avg"),
            n=Count("ts"),
        )
        .order_by()
    )
    objs = [
        MetricHourly(
            station_id=r["station_id"],
            metric_key=r["metric_key"],
            ts=r["hour"],
            min=r["mn"],
            max=r["mx"],
            avg=r["av"],
            count=r["n"],
        )
        for r in rows
    ]
    if objs:
        MetricHourly.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["station", "metric_key", "ts"],
            update_fields=["min", "max", "avg", "count"],
        )
    return len(objs)


def compact(now: datetime | None = None, *, chunk_hours: int = 24) -> int:
    """Compact all closed hours since the watermark into ``MetricHourly``.

    Covers ``[start, hour_floor(now - COMPACT_GRACE))`` (see ``COMPACT_GRACE``) with
    ``start = HistoryState.compacted_until`` or — first run — the hour of the oldest raw
    sample (no samples → 0, state stays ``None``). Works in chunks of ``chunk_hours``; each
    chunk (upsert + watermark) is one transaction, so a crash after a long outage keeps the
    progress made. Re-running is idempotent
    (upsert recomputes from raw). Exceptions propagate (``run_maintenance`` isolates them).
    Returns the number of hourly rows upserted.
    """
    if chunk_hours <= 0:
        raise ValueError("chunk_hours must be positive")
    end = hour_floor(_utc(now) - COMPACT_GRACE)
    state = HistoryState.get()
    start = state.compacted_until
    if start is None:
        oldest = MetricSample.objects.order_by("ts").values_list("ts", flat=True).first()
        if oldest is None:
            return 0
        start = hour_floor(oldest)
    else:
        start = hour_floor(start)
    step = timedelta(hours=chunk_hours)
    total = 0
    while start < end:
        chunk_end = min(start + step, end)
        with transaction.atomic():
            total += _compact_chunk(start, chunk_end)
            HistoryState.objects.filter(pk=state.pk).update(compacted_until=chunk_end)
        start = chunk_end
    return total


# --- partitions ----------------------------------------------------------------------------


def ensure_future_partitions(now: datetime | None = None) -> list[str]:
    """Create the current + next UTC month partitions of both tables (no-op off Postgres).

    Runs in one transaction with ``lock_timeout = DDL_LOCK_TIMEOUT`` (see there).
    """
    first = partitions.month_start(_utc(now))
    created = []
    with transaction.atomic():
        _set_ddl_lock_timeout()
        for table in partitions.PARTITIONED_TABLES:
            created += partitions.ensure_partitions(connection, table, first, 2)
    return created


# --- retention -----------------------------------------------------------------------------


def _raw_partition_safe(lo: datetime, hi: datetime, compacted_until: datetime | None) -> bool:
    if compacted_until is not None:
        return hi <= compacted_until
    # Never compacted: only safe if there is nothing to lose.
    return not MetricSample.objects.filter(ts__gte=lo, ts__lt=hi).exists()


def _warn_uncompacted(name: str, compacted_until: datetime | None) -> None:
    logger.warning(
        "history: keeping expired raw partition %s — not fully compacted (compacted_until=%s)",
        name,
        compacted_until,
    )


def _drop_expired_partitioned(now: datetime, compacted_until: datetime | None) -> list[str]:
    raw_cutoff = now - timedelta(days=settings.HISTORY_RAW_RETENTION_DAYS)
    hourly_cutoff = now - timedelta(days=settings.HISTORY_HOURLY_RETENTION_DAYS)
    dropped = []
    for name, lo, hi in partitions.list_partitions(connection, RAW_TABLE):
        if hi > raw_cutoff:
            continue
        if not _raw_partition_safe(lo, hi, compacted_until):
            _warn_uncompacted(name, compacted_until)
            continue
        partitions.drop_partition(connection, name)
        dropped.append(name)
    for name, _lo, hi in partitions.list_partitions(connection, HOURLY_TABLE):
        if hi <= hourly_cutoff:
            partitions.drop_partition(connection, name)
            dropped.append(name)
    return dropped


def _months_with_rows(model, before: datetime) -> list[datetime]:
    """UTC month starts (ascending) that hold rows of ``model`` with ``ts < before``."""
    oldest = (
        model.objects.filter(ts__lt=before).order_by("ts").values_list("ts", flat=True).first()
    )
    if oldest is None:
        return []
    months = []
    month = partitions.month_start(oldest)
    while month < before:
        nxt = partitions.add_months(month, 1)
        if model.objects.filter(ts__gte=month, ts__lt=nxt).exists():
            months.append(month)
        month = nxt
    return months


def _drop_expired_rows(now: datetime, compacted_until: datetime | None) -> list[str]:
    """Non-partitioned fallback: delete the rows the partition drop would remove."""
    dropped = []
    # A raw "partition" [lo, hi) is expired when hi <= cutoff, i.e. lo < month_start(cutoff).
    raw_limit = partitions.month_start(now - timedelta(days=settings.HISTORY_RAW_RETENTION_DAYS))
    for lo in _months_with_rows(MetricSample, raw_limit):
        hi = partitions.add_months(lo, 1)
        name = partitions.partition_name(RAW_TABLE, lo)
        if not _raw_partition_safe(lo, hi, compacted_until):
            _warn_uncompacted(name, compacted_until)
            # Months are ascending; later ones end even later → also unsafe.
            break
        MetricSample.objects.filter(ts__gte=lo, ts__lt=hi).delete()
        dropped.append(name)
    hourly_limit = partitions.month_start(
        now - timedelta(days=settings.HISTORY_HOURLY_RETENTION_DAYS)
    )
    for lo in _months_with_rows(MetricHourly, hourly_limit):
        MetricHourly.objects.filter(ts__gte=lo, ts__lt=partitions.add_months(lo, 1)).delete()
        dropped.append(partitions.partition_name(HOURLY_TABLE, lo))
    return dropped


def drop_expired(now: datetime | None = None) -> list[str]:
    """Drop expired raw/hourly months; returns partition names dropped.

    Raw: partition end ``<= now - HISTORY_RAW_RETENTION_DAYS`` **and** ``<= compacted_until``
    (otherwise kept with a warning; with no watermark at all only empty partitions go).
    Hourly: partition end ``<= now - HISTORY_HOURLY_RETENTION_DAYS``.
    On PostgreSQL the drops run with ``lock_timeout = DDL_LOCK_TIMEOUT`` (see there).
    """
    now = _utc(now)
    compacted_until = HistoryState.get().compacted_until
    if partitions.is_partitioned(connection):
        with transaction.atomic():
            _set_ddl_lock_timeout()
            return _drop_expired_partitioned(now, compacted_until)
    return _drop_expired_rows(now, compacted_until)


# --- orchestration -------------------------------------------------------------------------


def run_maintenance(now: datetime | None = None) -> dict:
    """compact → ensure partitions → drop expired; each step error-isolated.

    Returns ``{"compacted": int, "created": [...], "dropped": [...], "errors": [step, ...]}``.
    """
    now = _utc(now)
    result = {"compacted": 0, "created": [], "dropped": [], "errors": []}

    # compact() commits per chunk itself; no outer atomic (it would undo chunk progress).
    try:
        result["compacted"] = compact(now=now)
    except Exception:
        logger.exception("history: compaction failed")
        result["errors"].append("compact")

    try:
        with transaction.atomic():
            result["created"] = ensure_future_partitions(now=now)
    except Exception:
        logger.exception("history: creating partitions failed")
        result["errors"].append("ensure_partitions")

    if "compact" in result["errors"]:
        # Still safe (drop_expired guards on the watermark), just noted.
        logger.warning("history: dropping with a stale compaction watermark")
    try:
        with transaction.atomic():
            result["dropped"] = drop_expired(now=now)
    except Exception:
        logger.exception("history: dropping expired data failed")
        result["errors"].append("drop_expired")

    return result
