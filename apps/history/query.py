"""Read side of the metric history: resolution choice + series fetch/downsampling.

Resolutions and their source table:

- ``raw`` (one row per heartbeat; budgeted at the worst-case 10 s interval) and ``5m`` (DB
  aggregate per 300 s UTC epoch bucket) are served from ``MetricSample`` — but only while
  ``since`` lies inside the raw window (``HISTORY_RAW_RETENTION_DAYS``, default 30 d). Older
  ranges fall back to ``1h`` from ``MetricHourly`` (otherwise the answer would be empty).
- ``1h`` (rows as is) and ``1d`` (DB aggregate per UTC day, count-weighted avg) are served from
  ``MetricHourly`` (kept ``HISTORY_HOURLY_RETENTION_DAYS``, default 730 d). Hours not yet
  compacted by ``history_maintain`` are not in there yet.

Retention settings are read per call (``max_span()``), never frozen at import.
Hidden/blocked metric definitions are never returned (``visible_keys``), like ``/telemetry/``.

All bucketing is in UTC (project ``TIME_ZONE`` is Europe/Vienna — never truncate locally).
Points are ``[epoch_seconds:int, min, avg, max]``, sorted by time.
"""

from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db.models import Avg, BigIntegerField, F, FloatField, Func, Max, Min, Q, Sum
from django.db.models.functions import TruncDay

from apps.history.models import MetricHourly, MetricSample

# Bucket width per resolution in seconds. ``raw`` has no buckets (one row per heartbeat);
# its value is the *worst-case* step for point estimates (auto choice, HARD_MAX_POINTS):
# the agent's minimum ``heartbeat_interval`` is 10 s (``station_agent/config.py``), the
# default 60 s — estimating with 60 would let a 10 s station return 6x the limit.
RESOLUTIONS = {"raw": 10, "5m": 300, "1h": 3600, "1d": 86400}
RESOLUTION_CHOICES = (*RESOLUTIONS, "auto")
MAX_POINTS = 1000  # auto: target maximum points per series
HARD_MAX_POINTS = 5000  # explicit resolution: hard limit per series (else 400)
MAX_KEYS = 10  # keys per request
DEFAULT_SPAN = timedelta(hours=24)

_HOURLY_RESOLUTIONS = ("1h", "1d")


def max_span() -> timedelta:
    """Longest allowed ``until - since``: ``HISTORY_HOURLY_RETENTION_DAYS`` (read per call)."""
    return timedelta(days=settings.HISTORY_HOURLY_RETENTION_DAYS)


def visible_keys(keys: list[str]) -> list[str]:
    """``keys`` without those whose definition is hidden or blocked (order kept).

    Parity with the current-values list (``/telemetry/``), which never shows them. Keys
    without a definition stay (they are shown with fallback metadata).
    """
    from apps.monitoring.models import MetricDefinition

    if not keys:
        return []
    invisible = set(
        MetricDefinition.objects.filter(key__in=keys)
        .filter(Q(is_hidden=True) | Q(is_blocked=True))
        .values_list("key", flat=True)
    )
    return [k for k in keys if k not in invisible]


class _EpochBucket(Func):
    """UTC epoch seconds (int) of ``ts`` floored to a multiple of ``step`` seconds."""

    output_field = BigIntegerField()

    def __init__(self, expression, step: int):
        self.step = int(step)  # our constant, never user input — safe to inline
        super().__init__(expression)

    def as_sql(self, compiler, connection, **extra_context):
        return super().as_sql(
            compiler,
            connection,
            # '%%%%s': one level for the template %-formatting, one for the SQLite
            # backend's %s→? conversion (which turns '%%' back into '%').
            template=(
                f"((CAST(strftime('%%%%s', %(expressions)s) AS INTEGER) / {self.step}) "
                f"* {self.step})"
            ),
            **extra_context,
        )

    def as_postgresql(self, compiler, connection, **extra_context):
        return super().as_sql(
            compiler,
            connection,
            template=(
                f"CAST(EXTRACT(EPOCH FROM date_bin('{self.step} seconds', %(expressions)s, "
                "TIMESTAMPTZ '1970-01-01 00:00:00+00')) AS bigint)"
            ),
            **extra_context,
        )


def default_keys(station) -> list[str]:
    """Keys of the station's current ``telemetry.metrics`` with a visible definition.

    Same rule as the current-values list (``StationTelemetrySerializer.metrics``): defined,
    not hidden, not blocked. Ordered by (group, key).
    """
    from apps.monitoring.models import MetricDefinition
    from apps.stations.models import StationTelemetry

    metrics = (
        StationTelemetry.objects.filter(station=station).values_list("metrics", flat=True).first()
    )
    if not isinstance(metrics, dict) or not metrics:
        return []
    return list(
        MetricDefinition.objects.filter(key__in=list(metrics), is_hidden=False, is_blocked=False)
        .order_by("group", "key")
        .values_list("key", flat=True)[:MAX_KEYS]
    )


def expected_points(resolution: str, since: datetime, until: datetime) -> int:
    """Nominal number of points per series for ``resolution`` over ``[since, until]``."""
    return int((until - since).total_seconds() // RESOLUTIONS[resolution])


def choose_resolution(
    requested: str, since: datetime, until: datetime, now: datetime
) -> tuple[str, str]:
    """``(resolution, source)`` for a request; source is ``"raw"`` or ``"hourly"``.

    ``auto`` = finest resolution with at most ``MAX_POINTS`` points (``1d`` if none fits).
    A sub-hour resolution whose ``since`` is older than the raw retention window is served
    as ``1h`` from the hourly table.
    """
    if requested == "auto":
        resolution = next(
            (r for r in RESOLUTIONS if expected_points(r, since, until) <= MAX_POINTS), "1d"
        )
    elif requested in RESOLUTIONS:
        resolution = requested
    else:
        raise ValueError(f"unknown resolution: {requested!r}")
    if resolution in _HOURLY_RESOLUTIONS:
        return resolution, "hourly"
    raw_window_start = now - timedelta(days=settings.HISTORY_RAW_RETENTION_DAYS)
    if since < raw_window_start:
        return "1h", "hourly"
    return resolution, "raw"


def _floor(dt: datetime, step: int) -> datetime:
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step, tz=UTC)


def _empty(keys):
    return {k: [] for k in keys}


def _rows(model, station, keys, lower, until):
    return (
        model.objects.filter(station=station, metric_key__in=keys, ts__gte=lower, ts__lte=until)
        .order_by("metric_key", "ts")
        .values_list("metric_key", "ts", "min", "avg", "max")
    )


def fetch_series(
    station, keys: list[str], since: datetime, until: datetime, resolution: str, source: str
) -> dict[str, list[list]]:
    """``{key: [[epoch_s, min, avg, max], ...]}`` for every requested key (empty if no data).

    For bucketed resolutions the lower bound is floored to the bucket start, so the first
    bucket is complete rather than cut at ``since``.
    """
    out = _empty(keys)
    if not keys:
        return out
    step = RESOLUTIONS[resolution]

    if source == "raw" and resolution == "raw":
        for key, ts, v_min, v_avg, v_max in _rows(MetricSample, station, keys, since, until):
            out[key].append([int(ts.timestamp()), v_min, v_avg, v_max])
        return out

    if source == "raw" and resolution == "5m":
        # Bucketed in SQL (rows pulled = buckets, not heartbeats): min-of-mins,
        # max-of-maxs, avg-of-avgs (one raw row per heartbeat, equal weight).
        lower = _floor(since, step)
        rows = (
            MetricSample.objects.filter(
                station=station, metric_key__in=keys, ts__gte=lower, ts__lte=until
            )
            .annotate(bucket=_EpochBucket("ts", step))
            .values("metric_key", "bucket")
            .annotate(b_min=Min("min"), b_avg=Avg("avg"), b_max=Max("max"))
            .order_by("metric_key", "bucket")
        )
        for r in rows:
            out[r["metric_key"]].append([int(r["bucket"]), r["b_min"], r["b_avg"], r["b_max"]])
        return out

    if source == "hourly" and resolution == "1h":
        lower = _floor(since, step)
        for key, ts, v_min, v_avg, v_max in _rows(MetricHourly, station, keys, lower, until):
            out[key].append([int(ts.timestamp()), v_min, v_avg, v_max])
        return out

    if source == "hourly" and resolution == "1d":
        lower = _floor(since, step)
        rows = (
            MetricHourly.objects.filter(
                station=station, metric_key__in=keys, ts__gte=lower, ts__lte=until
            )
            .annotate(day=TruncDay("ts", tzinfo=UTC))
            .values("metric_key", "day")
            .annotate(
                d_min=Min("min"),
                d_max=Max("max"),
                d_wsum=Sum(F("avg") * F("count"), output_field=FloatField()),
                d_count=Sum("count"),
            )
            .order_by("metric_key", "day")
        )
        for r in rows:
            if not r["d_count"]:
                continue  # count is >= 1 for every compacted hour; guard against division by 0
            out[r["metric_key"]].append(
                [
                    int(r["day"].timestamp()),
                    r["d_min"],
                    r["d_wsum"] / r["d_count"],
                    r["d_max"],
                ]
            )
        return out

    raise ValueError(f"unsupported resolution/source: {resolution!r}/{source!r}")
