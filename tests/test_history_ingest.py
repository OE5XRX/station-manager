"""History ingest: accepted metric values are recorded per heartbeat (spec §2).

Runs on SQLite (normal suite) and on PostgreSQL (``test-history-postgres`` CI job);
the missing-partition test is PG-only.
"""

import json
import logging
from datetime import datetime

import pytest
from django.core.cache import cache
from django.db import connection, transaction
from django.test import Client
from django.utils import timezone

from apps.history import partitions
from apps.history.ingest import INGEST_ERROR_CACHE_KEY, record_samples
from apps.history.models import MetricSample
from apps.monitoring.metrics_ingest import ingest_metrics
from apps.monitoring.models import MetricDefinition
from apps.stations.models import StationTelemetry
from tests.conftest import device_auth_headers

pytestmark = pytest.mark.django_db

KEY = "pwr.12v.voltage_v"
VALUE = {"min": 11.8, "max": 12.3, "avg": 12.1}


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _ingest(station, metrics=None, inventory=None):
    return ingest_metrics(
        station,
        metrics=metrics,
        descriptors=None,
        descriptors_hash=None,
        agent_version="0.1.0",
        inventory=inventory,
        telemetry=None,
    )


def _define(key, **kw):
    return MetricDefinition.objects.create(key=key, label=key, **kw)


def _stored(station):
    return StationTelemetry.objects.get(station=station).metrics


def _fail_bulk_create(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("history insert failed")

    monkeypatch.setattr(MetricSample.objects, "bulk_create", boom)


def _ingest_errors(caplog):
    return [
        r for r in caplog.records if r.name == "apps.history.ingest" and r.levelno >= logging.ERROR
    ]


# --- accepted vs. rejected --------------------------------------------------


def test_accepted_values_recorded(station):
    _define(KEY)
    _ingest(station, metrics={KEY: VALUE})
    sample = MetricSample.objects.get(station=station, metric_key=KEY)
    assert (sample.min, sample.max, sample.avg) == (11.8, 12.3, 12.1)
    assert sample.ts == datetime.fromisoformat(_stored(station)[KEY]["ts"])


def test_blocked_not_recorded(station):
    _define(KEY, is_blocked=True)
    _ingest(station, metrics={KEY: VALUE})
    assert not MetricSample.objects.exists()


def test_out_of_range_not_recorded(station):
    _define(KEY, min_value=0, max_value=20)
    _ingest(station, metrics={KEY: 25})
    assert not MetricSample.objects.exists()


def test_undefined_key_not_recorded(station):
    _ingest(station, metrics={KEY: VALUE})
    assert KEY in _stored(station)
    assert not MetricSample.objects.exists()


def test_hidden_is_recorded(station):
    _define(KEY, is_hidden=True)
    _ingest(station, metrics={KEY: VALUE})
    assert MetricSample.objects.filter(station=station, metric_key=KEY).count() == 1


def test_legacy_inventory_values_recorded(station):
    # Legacy definitions are seeded by monitoring migration 0006.
    assert MetricDefinition.objects.filter(key="cpu.temp_c", is_blocked=False).exists()
    _ingest(station, inventory={"cpu": {"temperature_c": 50}})
    sample = MetricSample.objects.get(station=station, metric_key="cpu.temp_c")
    assert (sample.min, sample.max, sample.avg) == (50.0, 50.0, 50.0)


def test_only_accepted_keys_of_mixed_heartbeat_recorded(station):
    _define(KEY)
    _define("cpu.usage_pct", is_blocked=True)
    _ingest(station, metrics={KEY: VALUE, "cpu.usage_pct": 3, "x.undefined_v": 1})
    assert list(MetricSample.objects.values_list("metric_key", flat=True)) == [KEY]


def test_no_accepted_values_no_insert(station, django_assert_num_queries):
    with django_assert_num_queries(0):
        assert record_samples(station, timezone.now(), {}) == 0


# --- record_samples ---------------------------------------------------------


def test_record_samples_returns_count(station):
    ts = timezone.now()  # current month: its partition always exists on PG
    assert record_samples(station, ts, {KEY: VALUE, "b.c_v": VALUE}) == 2
    assert set(MetricSample.objects.values_list("metric_key", "ts")) == {(KEY, ts), ("b.c_v", ts)}


def test_duplicate_heartbeat_same_ts_ignored(station):
    ts = timezone.now()
    record_samples(station, ts, {KEY: VALUE})
    record_samples(station, ts, {KEY: {"min": 1.0, "max": 2.0, "avg": 1.5}})
    assert MetricSample.objects.count() == 1
    assert MetricSample.objects.get().avg == 12.1


# --- error isolation --------------------------------------------------------


def test_history_error_isolated(station, monkeypatch, caplog):
    _define(KEY)
    _fail_bulk_create(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="apps.history.ingest")

    _ingest(station, metrics={KEY: VALUE})
    assert _stored(station)[KEY]["avg"] == 12.1
    assert record_samples(station, timezone.now(), {KEY: VALUE}) == 0

    # Two failures, but only one error-level log per hour.
    assert len(_ingest_errors(caplog)) == 1
    assert cache.get(INGEST_ERROR_CACHE_KEY) is not None


def test_history_error_keeps_outer_transaction_usable(station, monkeypatch):
    _define(KEY)
    _fail_bulk_create(monkeypatch)
    with transaction.atomic():
        _ingest(station, metrics={KEY: VALUE})
        # A follow-up query in the same (outer) transaction still works.
        assert StationTelemetry.objects.filter(station=station).count() == 1
        assert MetricDefinition.objects.filter(key=KEY).exists()


# --- heartbeat endpoint -----------------------------------------------------


def _post(station, private_key, **extra):
    body = {
        "hostname": "h",
        "os_version": "o",
        "uptime": 5.0,
        "module_versions": {},
        "ip_address": "10.0.0.2",
        "agent_version": "0.1.0",
        **extra,
    }
    body_bytes = json.dumps(body).encode()
    headers = device_auth_headers(private_key, station.id, body_bytes)
    return Client().post(
        "/api/v1/heartbeat/", data=body_bytes, content_type="application/json", **headers
    )


def test_heartbeat_records_sample(station_with_key):
    station, key = station_with_key
    _define(KEY)
    resp = _post(station, key, metrics={KEY: VALUE})
    assert resp.status_code == 200
    assert MetricSample.objects.filter(station=station, metric_key=KEY).count() == 1


def test_heartbeat_200_when_history_fails(station_with_key, monkeypatch):
    station, key = station_with_key
    _define(KEY)
    _fail_bulk_create(monkeypatch)
    resp = _post(station, key, metrics={KEY: VALUE})
    assert resp.status_code == 200
    assert _stored(station)[KEY]["avg"] == 12.1
    assert not MetricSample.objects.exists()


# --- Postgres: rollout gap (partition missing) ------------------------------


def test_missing_partition_isolated(require_postgres, station, caplog):
    _define(KEY)
    month = partitions.month_start(timezone.now())
    partitions.drop_partition(connection, partitions.partition_name("history_metricsample", month))
    caplog.set_level(logging.DEBUG, logger="apps.history.ingest")

    with transaction.atomic():
        _ingest(station, metrics={KEY: VALUE})
        _ingest(station, metrics={KEY: VALUE})
        # Outer transaction still usable after the failed partition insert.
        assert _stored(station)[KEY]["avg"] == 12.1

    assert not MetricSample.objects.exists()
    assert len(_ingest_errors(caplog)) == 1
