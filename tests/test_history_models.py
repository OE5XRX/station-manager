"""History models: composite PK, conflict handling, cascade, migration state (SQLite + PG)."""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command

from apps.history.models import HistoryState, MetricHourly, MetricSample

pytestmark = pytest.mark.django_db


def _now_hour():
    # Inside the migration-created partition window on Postgres (current month).
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def test_create_sample(station):
    ts = _now_hour()
    MetricSample.objects.create(
        station=station, metric_key="pwr.12v.voltage_v", ts=ts, min=11.8, max=12.3, avg=12.1
    )
    row = MetricSample.objects.get()
    assert row.pk == (station.pk, "pwr.12v.voltage_v", ts)
    assert (row.min, row.max, row.avg) == (11.8, 12.3, 12.1)


def test_duplicate_sample_ignored(station):
    ts = _now_hour()
    for _ in range(2):
        MetricSample.objects.bulk_create(
            [MetricSample(station=station, metric_key="cpu.temp_c", ts=ts, min=1, max=2, avg=1.5)],
            ignore_conflicts=True,
        )
    assert MetricSample.objects.count() == 1


def test_hourly_upsert_second_values_win(station):
    ts = _now_hour()
    for vals in ((1.0, 3.0, 2.0, 5), (0.5, 4.0, 2.5, 7)):
        MetricHourly.objects.bulk_create(
            [
                MetricHourly(
                    station=station,
                    metric_key="cpu.temp_c",
                    ts=ts,
                    min=vals[0],
                    max=vals[1],
                    avg=vals[2],
                    count=vals[3],
                )
            ],
            update_conflicts=True,
            unique_fields=["station", "metric_key", "ts"],
            update_fields=["min", "max", "avg", "count"],
        )
    row = MetricHourly.objects.get()
    assert (row.min, row.max, row.avg, row.count) == (0.5, 4.0, 2.5, 7)


def test_history_state_singleton():
    state = HistoryState.get()
    assert state.pk == 1
    assert state.compacted_until is None
    assert HistoryState.get().pk == 1
    assert HistoryState.objects.count() == 1


def test_station_delete_cascades(station_factory):
    a, b = station_factory(), station_factory()
    ts = _now_hour()
    for st in (a, b):
        MetricSample.objects.create(station=st, metric_key="k.v", ts=ts, min=1, max=1, avg=1)
        MetricHourly.objects.create(
            station=st, metric_key="k.v", ts=ts - timedelta(hours=1), min=1, max=1, avg=1, count=1
        )
    a.delete()
    assert list(MetricSample.objects.values_list("station_id", flat=True)) == [b.pk]
    assert list(MetricHourly.objects.values_list("station_id", flat=True)) == [b.pk]


def test_no_missing_migrations():
    # Raises SystemExit(1) if the model state drifted from the migrations.
    call_command("makemigrations", "history", "--check", "--dry-run", verbosity=0)
