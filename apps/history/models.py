"""Time-series storage for generic station metrics.

``MetricSample`` (raw, one row per accepted value per heartbeat) and ``MetricHourly``
(compacted hours) are monthly RANGE-partitioned by ``ts`` on PostgreSQL — the tables are
created by hand-written DDL in ``migrations/0001_initial.py``, plain tables on SQLite. The
model definitions here must therefore stay in sync with that DDL (column names/types, PK).

Both use a composite primary key ``(station, metric_key, ts)``: Postgres requires the
partition key to be part of every unique constraint, and the PK doubles as the index for
the only access pattern (one station, some keys, a time range). That is why the station FK
carries no separate index (``db_index=False``) — the PK prefix covers it.

PostgreSQL only: the migration adds a BRIN index on ``ts`` to both partitioned parents
(``<table>_ts_brin``, inherited by every partition) for time-only scans (compaction,
retention). It is deliberately not part of the model state, so SQLite has no such index.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from station_agent.metrics_contract import MAX_KEY_LEN


class _SeriesBase(models.Model):
    pk = models.CompositePrimaryKey("station", "metric_key", "ts")
    station = models.ForeignKey(
        "stations.Station",
        on_delete=models.CASCADE,
        related_name="+",
        db_index=False,
        verbose_name=_("station"),
    )
    metric_key = models.CharField(_("metric key"), max_length=MAX_KEY_LEN)
    ts = models.DateTimeField(_("timestamp"))
    min = models.FloatField(_("minimum"))
    max = models.FloatField(_("maximum"))
    avg = models.FloatField(_("average"))

    class Meta:
        abstract = True


class MetricSample(_SeriesBase):
    """One accepted metric value of one heartbeat (raw, kept ~30 days)."""

    class Meta:
        db_table = "history_metricsample"
        verbose_name = _("metric sample")
        verbose_name_plural = _("metric samples")


class MetricHourly(_SeriesBase):
    """Hourly compaction of ``MetricSample`` (kept ~2 years). ``ts`` = UTC hour start."""

    count = models.PositiveIntegerField(_("sample count"))

    class Meta:
        db_table = "history_metrichourly"
        verbose_name = _("hourly metric aggregate")
        verbose_name_plural = _("hourly metric aggregates")


class HistoryState(models.Model):
    """Singleton (pk=1) bookkeeping for ``history_maintain``.

    ``compacted_until``: every hour strictly before this instant has been compacted into
    ``MetricHourly``. Raw partitions are only dropped when they end at or before it.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    compacted_until = models.DateTimeField(_("compacted until"), null=True, blank=True)

    class Meta:
        verbose_name = _("history state")
        verbose_name_plural = _("history state")

    def __str__(self):
        return f"HistoryState(compacted_until={self.compacted_until})"

    @classmethod
    def get(cls) -> "HistoryState":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj
