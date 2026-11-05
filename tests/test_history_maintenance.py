"""History maintenance: hourly compaction, partition upkeep, retention, command (SQLite + PG).

All tests use a fixed ``now``. On Postgres the months they write into are created up front
(``_partitions``); partition DDL inside the test transaction is rolled back with it.
"""

import logging
from datetime import UTC, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.db import connection

from apps.history import maintenance
from apps.history.models import HistoryState, MetricHourly, MetricSample
from apps.history.partitions import (
    PARTITIONED_TABLES,
    add_months,
    ensure_partitions,
    list_partitions,
    partition_name,
)

pytestmark = pytest.mark.django_db

RAW = "history_metricsample"
HOURLY = "history_metrichourly"
NOW = datetime(2026, 11, 5, 11, 20, tzinfo=UTC)
KEY = "pwr.12v.voltage_v"


@pytest.fixture(autouse=True)
def _partitions(db):
    # Sept 2026 … Dec 2026 for both tables, plus the old hourly months used by the
    # hourly-retention test (no-op on SQLite).
    for table in PARTITIONED_TABLES:
        ensure_partitions(connection, table, datetime(2026, 9, 1, tzinfo=UTC), 4)
    ensure_partitions(connection, HOURLY, datetime(2024, 10, 1, tzinfo=UTC), 2)


def _fire_deferred_fks():
    # The FK to stations_station is DEFERRABLE INITIALLY DEFERRED; rows inserted in the
    # (test) transaction leave pending trigger events, and Postgres refuses to DROP a
    # partition with pending events. In production the rows were committed long ago.
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _sample(station, ts, mn, mx, avg, key=KEY):
    row = MetricSample.objects.create(
        station=station, metric_key=key, ts=ts, min=mn, max=mx, avg=avg
    )
    _fire_deferred_fks()
    return row


def _hourly(station, ts, key=KEY):
    row = MetricHourly.objects.create(
        station=station, metric_key=key, ts=ts, min=1, max=2, avg=1.5, count=1
    )
    _fire_deferred_fks()
    return row


def _set_compacted(dt):
    state = HistoryState.get()
    state.compacted_until = dt
    state.save()


def _at(h, m=0, day=5, month=11):
    return datetime(2026, month, day, h, m, tzinfo=UTC)


# --- compaction --------------------------------------------------------------------------


def test_compact_closed_hour_only(station):
    _sample(station, _at(10, 5), 11, 13, 12)
    _sample(station, _at(10, 35), 10, 12.5, 11)
    _sample(station, _at(11, 10), 9, 9, 9)

    n = maintenance.compact(now=NOW)

    assert n == 1
    row = MetricHourly.objects.get()
    assert row.ts == _at(10)
    assert (row.min, row.max, row.avg, row.count) == (10, 13, 11.5, 2)
    assert not MetricHourly.objects.filter(ts=_at(11)).exists()
    assert HistoryState.get().compacted_until == _at(11)


def test_compact_idempotent_then_next_hour(station):
    _sample(station, _at(10, 5), 11, 13, 12)
    _sample(station, _at(10, 35), 10, 12.5, 11)
    _sample(station, _at(11, 10), 9, 9, 9)
    maintenance.compact(now=NOW)

    assert maintenance.compact(now=NOW) == 0
    assert MetricHourly.objects.count() == 1
    assert MetricHourly.objects.get().avg == 11.5

    _sample(station, _at(11, 50), 8, 10, 9.5)
    # 12:06 = past the hour boundary + COMPACT_GRACE, so hour 11 is compacted now.
    assert maintenance.compact(now=_at(12, 6)) == 1
    row = MetricHourly.objects.get(ts=_at(11))
    assert (row.min, row.max, row.avg, row.count) == (8, 10, 9.25, 2)
    assert HistoryState.get().compacted_until == _at(12)


def test_compact_grace_skips_just_closed_hour(station):
    # Ingest stamps ts=now before its INSERT commits; right after the hour boundary such a
    # row may still be invisible — the watermark must not pass the hour yet.
    _set_compacted(_at(10))
    _sample(station, datetime(2026, 11, 5, 10, 59, 30, tzinfo=UTC), 1, 3, 2)

    assert maintenance.compact(now=_at(11, 2)) == 0
    assert not MetricHourly.objects.exists()
    assert HistoryState.get().compacted_until <= _at(10)

    assert maintenance.compact(now=_at(11, 6)) == 1
    assert MetricHourly.objects.get().ts == _at(10)
    assert HistoryState.get().compacted_until == _at(11)


def test_compact_grace_first_run(station):
    # Same without a watermark: the first run must not move past the open grace window.
    _sample(station, datetime(2026, 11, 5, 10, 59, 30, tzinfo=UTC), 1, 3, 2)

    assert maintenance.compact(now=_at(11, 2)) == 0
    assert HistoryState.get().compacted_until is None
    assert maintenance.compact(now=_at(11, 6)) == 1


def test_compact_separates_stations_and_keys(station, station_factory):
    other = station_factory(name="other")
    _sample(station, _at(9, 1), 1, 1, 1)
    _sample(station, _at(9, 2), 3, 3, 3, key="cpu.temp_c")
    _sample(other, _at(9, 3), 5, 5, 5)

    assert maintenance.compact(now=NOW) == 3
    assert MetricHourly.objects.get(station=other).avg == 5
    assert MetricHourly.objects.get(station=station, metric_key="cpu.temp_c").avg == 3


def test_compact_buckets_in_utc_for_vienna_input(station):
    vienna = ZoneInfo("Europe/Vienna")
    # 11:05 Vienna (CET, +01:00) == 10:05 UTC → UTC hour 10:00.
    _sample(station, datetime(2026, 11, 5, 11, 5, tzinfo=vienna), 1, 2, 1.5)

    maintenance.compact(now=NOW)

    assert MetricHourly.objects.get().ts == _at(10)


def test_compact_nothing_leaves_state_none(db):
    assert maintenance.compact(now=NOW) == 0
    assert HistoryState.get().compacted_until is None


def test_compact_starts_at_oldest_sample_hour(station):
    _sample(station, _at(3, 59, day=4), 1, 1, 1)

    maintenance.compact(now=NOW)

    assert MetricHourly.objects.get().ts == _at(3, day=4)
    assert HistoryState.get().compacted_until == _at(11)


def test_compact_long_outage_bounded_chunks(station, monkeypatch):
    floor = _at(11)
    _set_compacted(floor - timedelta(days=5))
    _sample(station, floor - timedelta(days=4, minutes=30), 1, 1, 1)
    _sample(station, floor - timedelta(hours=2), 2, 2, 2)

    ranges = []
    real_chunk = maintenance._compact_chunk

    def spy(start, end):
        ranges.append((start, end))
        return real_chunk(start, end)

    monkeypatch.setattr(maintenance, "_compact_chunk", spy)

    assert maintenance.compact(now=NOW, chunk_hours=24) == 2

    assert len(ranges) == 5
    assert all(end - start <= timedelta(hours=24) for start, end in ranges)
    assert ranges[0][0] == floor - timedelta(days=5)
    assert ranges[-1][1] == floor
    assert HistoryState.get().compacted_until == floor


def test_compact_chunk_failure_keeps_progress(station, monkeypatch):
    floor = _at(11)
    _set_compacted(floor - timedelta(days=2))
    _sample(station, floor - timedelta(days=2) + timedelta(minutes=5), 1, 1, 1)
    real_chunk = maintenance._compact_chunk
    calls = []

    def flaky(start, end):
        calls.append(start)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real_chunk(start, end)

    monkeypatch.setattr(maintenance, "_compact_chunk", flaky)

    with pytest.raises(RuntimeError):
        maintenance.compact(now=NOW, chunk_hours=24)
    # First chunk committed its watermark, the failed one did not move it.
    assert HistoryState.get().compacted_until == floor - timedelta(days=1)
    assert MetricHourly.objects.count() == 1


# --- partitions ----------------------------------------------------------------------------


def test_ensure_future_partitions_creates_next_month(require_postgres):
    now = datetime(2027, 6, 10, tzinfo=UTC)
    names = maintenance.ensure_future_partitions(now=now)
    for table in PARTITIONED_TABLES:
        assert partition_name(table, now) in names
        assert partition_name(table, add_months(now, 1)) in names
        listed = {n for n, _lo, _hi in list_partitions(connection, table)}
        assert {partition_name(table, now), partition_name(table, add_months(now, 1))} <= listed
    assert maintenance.ensure_future_partitions(now=now) == []


def _lock_timeout():
    with connection.cursor() as cur:
        cur.execute("SHOW lock_timeout")
        return cur.fetchone()[0]


def test_ensure_future_partitions_sets_ddl_lock_timeout(require_postgres):
    # SET LOCAL lives until the end of the (test) transaction → observable afterwards.
    assert _lock_timeout() != maintenance.DDL_LOCK_TIMEOUT
    maintenance.ensure_future_partitions(now=NOW)
    assert _lock_timeout() == maintenance.DDL_LOCK_TIMEOUT


def test_drop_expired_sets_ddl_lock_timeout(require_postgres):
    assert _lock_timeout() != maintenance.DDL_LOCK_TIMEOUT
    maintenance.drop_expired(now=NOW)
    assert _lock_timeout() == maintenance.DDL_LOCK_TIMEOUT


def test_ensure_future_partitions_noop_on_sqlite(db):
    if connection.vendor == "postgresql":
        pytest.skip("SQLite-only")
    assert maintenance.ensure_future_partitions(now=NOW) == []


# --- retention -----------------------------------------------------------------------------

SEPT = datetime(2026, 9, 15, 12, tzinfo=UTC)
OCT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def test_drop_expired_raw(station):
    _sample(station, SEPT, 1, 1, 1)
    _sample(station, OCT, 2, 2, 2)
    _set_compacted(_at(11))

    dropped = maintenance.drop_expired(now=NOW)

    assert partition_name(RAW, SEPT) in dropped
    assert partition_name(RAW, OCT) not in dropped
    assert list(MetricSample.objects.values_list("avg", flat=True)) == [2]
    if connection.vendor == "postgresql":
        listed = {n for n, _lo, _hi in list_partitions(connection, RAW)}
        assert partition_name(RAW, SEPT) not in listed
        assert partition_name(RAW, OCT) in listed


def test_drop_expired_raw_guarded_by_watermark(station, caplog):
    _sample(station, SEPT, 1, 1, 1)
    _sample(station, OCT, 2, 2, 2)
    _set_compacted(datetime(2026, 9, 20, tzinfo=UTC))

    with caplog.at_level(logging.WARNING, logger="apps.history.maintenance"):
        dropped = maintenance.drop_expired(now=NOW)

    assert [n for n in dropped if n.startswith(RAW)] == []
    assert MetricSample.objects.count() == 2
    assert partition_name(RAW, SEPT) in caplog.text
    if connection.vendor == "postgresql":
        listed = {n for n, _lo, _hi in list_partitions(connection, RAW)}
        assert partition_name(RAW, SEPT) in listed


def test_drop_expired_raw_without_watermark_keeps_data(station, caplog):
    _sample(station, SEPT, 1, 1, 1)

    with caplog.at_level(logging.WARNING, logger="apps.history.maintenance"):
        dropped = maintenance.drop_expired(now=NOW)
    assert [n for n in dropped if n.startswith(RAW)] == []
    assert MetricSample.objects.count() == 1
    assert partition_name(RAW, SEPT) in caplog.text


def test_drop_expired_empty_raw_partition_without_watermark(require_postgres):
    # Fresh install that never received samples: old empty partitions may go.
    dropped = maintenance.drop_expired(now=NOW)
    assert partition_name(RAW, SEPT) in dropped
    assert HistoryState.get().compacted_until is None


def test_drop_expired_hourly(station):
    old = datetime(2024, 10, 20, tzinfo=UTC)  # partition end 2024-11-01: 734 d before NOW
    newer = datetime(2024, 11, 20, tzinfo=UTC)  # partition end 2024-12-01: 704 d before NOW
    _hourly(station, old)
    _hourly(station, newer)

    dropped = maintenance.drop_expired(now=NOW)

    assert partition_name(HOURLY, old) in dropped
    assert partition_name(HOURLY, newer) not in dropped
    assert list(MetricHourly.objects.values_list("ts", flat=True)) == [newer]


def test_retention_after_outage_never_passes_watermark(station):
    # Maintenance was down: watermark mid-Sept, raw Sept/Oct pending. A full run compacts
    # first, so afterwards Sept is compacted and may be dropped; Oct stays.
    _set_compacted(datetime(2026, 9, 15, tzinfo=UTC))
    _sample(station, SEPT, 1, 1, 1)
    _sample(station, OCT, 2, 2, 2)

    result = maintenance.run_maintenance(now=NOW)

    assert result["errors"] == []
    assert HistoryState.get().compacted_until == _at(11)
    assert partition_name(RAW, SEPT) in result["dropped"]
    assert MetricHourly.objects.filter(ts=SEPT).exists()
    assert MetricHourly.objects.filter(ts=OCT).exists()
    assert list(MetricSample.objects.values_list("avg", flat=True)) == [2]


# --- run_maintenance + command -------------------------------------------------------------


def test_run_maintenance_isolates_step_errors(db, monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("compact failed")

    monkeypatch.setattr(maintenance, "compact", boom)
    ensured = []
    monkeypatch.setattr(
        maintenance, "ensure_future_partitions", lambda now=None: ensured.append(now) or ["p"]
    )

    with caplog.at_level(logging.ERROR, logger="apps.history.maintenance"):
        result = maintenance.run_maintenance(now=NOW)

    assert ensured == [NOW]
    assert result["errors"] == ["compact"]
    assert result["created"] == ["p"]
    assert result["compacted"] == 0
    assert "compact failed" in caplog.text


def test_run_maintenance_db_error_keeps_transaction_usable(station, monkeypatch):
    # A DB error inside a step must not poison an outer transaction (PG semantics).
    def bad_ensure(now=None):
        with connection.cursor() as cur:
            cur.execute("SELECT * FROM table_that_does_not_exist")
        return []

    monkeypatch.setattr(maintenance, "ensure_future_partitions", bad_ensure)
    result = maintenance.run_maintenance(now=NOW)

    assert result["errors"] == ["ensure_partitions"]
    assert HistoryState.get() is not None


def test_command_runs_once(station):
    _sample(station, datetime.now(UTC) - timedelta(hours=2), 1, 2, 1.5)
    out = StringIO()

    call_command("history_maintain", stdout=out)

    text = out.getvalue()
    assert "compacted=" in text
    assert "errors=" in text
    assert MetricHourly.objects.count() == 1


def test_command_loop(db, monkeypatch):
    from apps.history.management.commands import history_maintain

    runs = []
    monkeypatch.setattr(
        history_maintain,
        "run_maintenance",
        lambda: runs.append(1) or {"compacted": 0, "created": [], "dropped": [], "errors": []},
    )

    class StopLoopError(Exception):
        pass

    def fake_sleep(seconds):
        assert seconds == 7
        if len(runs) >= 2:
            raise StopLoopError

    monkeypatch.setattr(history_maintain.time, "sleep", fake_sleep)
    with pytest.raises(StopLoopError):
        call_command("history_maintain", "--loop", "--interval", "7", stdout=StringIO())
    assert len(runs) == 2
