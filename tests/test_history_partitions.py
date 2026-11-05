"""Partition helpers: pure month math (any backend) + Postgres partition DDL."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import DataError, IntegrityError, connection, transaction

from apps.history import partitions
from apps.history.models import MetricSample

# --- pure helpers ---------------------------------------------------------


def test_month_start_utc():
    assert partitions.month_start(datetime(2026, 11, 5, 13, tzinfo=UTC)) == datetime(
        2026, 11, 1, tzinfo=UTC
    )


def test_month_start_converts_local_tz_to_utc():
    vienna = datetime(2026, 12, 1, 0, 30, tzinfo=ZoneInfo("Europe/Vienna"))
    # 00:30+01:00 == 2026-11-30 23:30 UTC → November partition.
    assert partitions.month_start(vienna) == datetime(2026, 11, 1, tzinfo=UTC)


def test_add_months():
    assert partitions.add_months(datetime(2026, 11, 1, tzinfo=UTC), 3) == datetime(
        2027, 2, 1, tzinfo=UTC
    )
    assert partitions.add_months(datetime(2026, 1, 1, tzinfo=UTC), -1) == datetime(
        2025, 12, 1, tzinfo=UTC
    )
    assert partitions.add_months(datetime(2026, 12, 1, tzinfo=UTC), 1) == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_partition_name():
    assert (
        partitions.partition_name("history_metricsample", datetime(2027, 2, 1, tzinfo=UTC))
        == "history_metricsample_p202702"
    )


def test_partitioned_tables_constant():
    assert partitions.PARTITIONED_TABLES == ("history_metricsample", "history_metrichourly")


@pytest.mark.django_db
def test_non_pg_helpers_are_noops():
    if partitions.is_partitioned(connection):
        pytest.skip("SQLite-only behaviour")
    first = datetime(2026, 11, 1, tzinfo=UTC)
    assert partitions.ensure_partitions(connection, "history_metricsample", first, 4) == []
    assert partitions.list_partitions(connection, "history_metricsample") == []


# --- Postgres -------------------------------------------------------------


def _expected_months():
    first = partitions.month_start(datetime.now(UTC))
    return [partitions.add_months(first, i) for i in range(4)]


def test_tables_are_partitioned(require_postgres):
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_partitioned_table pt "
            "JOIN pg_class c ON c.oid = pt.partrelid ORDER BY c.relname"
        )
        names = {r[0] for r in cur.fetchall()}
    assert set(partitions.PARTITIONED_TABLES) <= names


@pytest.mark.parametrize("table", partitions.PARTITIONED_TABLES)
def test_migration_created_current_plus_three_months(require_postgres, table):
    got = partitions.list_partitions(connection, table)
    months = _expected_months()
    assert [(n, lo, hi) for n, lo, hi in got] == [
        (partitions.partition_name(table, m), m, partitions.add_months(m, 1)) for m in months
    ]


def test_insert_outside_partitions_fails(require_postgres, station):
    far = partitions.add_months(partitions.month_start(datetime.now(UTC)), 6)
    with pytest.raises((IntegrityError, DataError)), transaction.atomic():
        MetricSample.objects.create(
            station=station, metric_key="k.v", ts=far + timedelta(days=2), min=1, max=1, avg=1
        )


def test_ensure_partitions_idempotent_and_drop(require_postgres):
    table = "history_metricsample"
    far = partitions.add_months(partitions.month_start(datetime.now(UTC)), 8)
    created = partitions.ensure_partitions(connection, table, far, 2)
    assert created == [
        partitions.partition_name(table, far),
        partitions.partition_name(table, partitions.add_months(far, 1)),
    ]
    assert partitions.ensure_partitions(connection, table, far, 2) == []
    # Overlap with existing migration-created partitions → only nothing new.
    assert (
        partitions.ensure_partitions(
            connection, table, partitions.month_start(datetime.now(UTC)), 4
        )
        == []
    )
    names = [n for n, _, _ in partitions.list_partitions(connection, table)]
    assert created[0] in names

    partitions.drop_partition(connection, created[0])
    names = [n for n, _, _ in partitions.list_partitions(connection, table)]
    assert created[0] not in names
    assert created[1] in names
    # Dropping a missing partition is a no-op.
    partitions.drop_partition(connection, created[0])


def test_list_partitions_ignores_foreign_names(require_postgres):
    with connection.cursor() as cur:
        cur.execute(
            "CREATE TABLE history_metricsample_odd PARTITION OF history_metricsample "
            "FOR VALUES FROM ('2040-01-01 00:00+00') TO ('2040-02-01 00:00+00')"
        )
    names = [n for n, _, _ in partitions.list_partitions(connection, "history_metricsample")]
    assert "history_metricsample_odd" not in names
