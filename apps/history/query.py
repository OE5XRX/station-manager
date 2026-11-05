"""Read side of the metric history: resolution choice + series fetch/downsampling.

Resolutions and their source table:

- ``raw`` (nominal 60 s, one row per heartbeat) and ``5m`` (raw rows bucketed in Python) are
  served from ``MetricSample`` — but only while ``since`` lies inside the raw retention window.
  Older ranges fall back to ``1h`` from ``MetricHourly`` (otherwise the answer would be empty).
- ``1h`` (rows as is) and ``1d`` (DB aggregate per UTC day, count-weighted avg) are served from
  ``MetricHourly``. Hours not yet compacted by ``history_maintain`` are not in there yet.

All bucketing is in UTC (project ``TIME_ZONE`` is Europe/Vienna — never truncate locally).
Points are ``[epoch_seconds:int, min, avg, max]``, sorted by time.
"""

from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db.models import F, FloatField, Max, Min, Sum
from django.db.models.functions import TruncDay

from apps.history.models import MetricHourly, MetricSample

# Bucket width per resolution in seconds (``raw`` is nominal: one heartbeat per minute).
RESOLUTIONS = {"raw": 60, "5m": 300, "1h": 3600, "1d": 86400}
RESOLUTION_CHOICES = (*RESOLUTIONS, "auto")
MAX_POINTS = 1000  # auto: target maximum points per series
HARD_MAX_POINTS = 5000  # explicit resolution: hard limit per series (else 400)
MAX_KEYS = 10  # keys per request
MAX_SPAN = timedelta(days=settings.HISTORY_HOURLY_RETENTION_DAYS)
DEFAULT_SPAN = timedelta(hours=24)

_HOURLY_RESOLUTIONS = ("1h", "1d")


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
        # Rows arrive sorted by (key, ts) → a bucket is complete when the key/bucket changes.
        # min-of-mins, max-of-maxs, avg-of-avgs (one raw row per heartbeat, equal weight).
        current = None  # [key, bucket, min, sum_avg, n, max]
        lower = _floor(since, step)
        for key, ts, v_min, v_avg, v_max in _rows(MetricSample, station, keys, lower, until):
            epoch = int(ts.timestamp())
            bucket = epoch - epoch % step
            if current is not None and current[0] == key and current[1] == bucket:
                current[2] = min(current[2], v_min)
                current[3] += v_avg
                current[4] += 1
                current[5] = max(current[5], v_max)
                continue
            if current is not None:
                out[current[0]].append(
                    [current[1], current[2], current[3] / current[4], current[5]]
                )
            current = [key, bucket, v_min, v_avg, 1, v_max]
        if current is not None:
            out[current[0]].append([current[1], current[2], current[3] / current[4], current[5]])
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
