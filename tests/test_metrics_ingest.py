"""ingest_metrics: value storage, descriptor upsert, legacy fallback, descriptors_required."""

import math

import pytest
from django.core.cache import cache

from apps.monitoring.metrics_ingest import ingest_metrics
from apps.monitoring.models import MAX_DEFINITIONS, MetricDefinition
from apps.stations.models import StationTelemetry
from station_agent.metrics_contract import MAX_KEYS, clean_descriptor, descriptors_hash

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _ingest(station, metrics=None, descriptors=None, descriptors_hash=None, **kw):
    return ingest_metrics(
        station,
        metrics=metrics,
        descriptors=descriptors,
        descriptors_hash=descriptors_hash,
        agent_version=kw.pop("agent_version", "0.1.0"),
        inventory=kw.pop("inventory", None),
        telemetry=kw.pop("telemetry", None),
    )


def _stored(station):
    return StationTelemetry.objects.get(station=station).metrics


def _desc(key, **kw):
    d = {"key": key, "label": key, "unit": "V", "group": "power", "precision": 2}
    d.update(kw)
    return d


def _define(key, **kw):
    return MetricDefinition.objects.create(key=key, label=key, **kw)


# --- values -----------------------------------------------------------------


def test_scalar_and_aggregate_values_stored(station):
    _ingest(
        station,
        metrics={
            "cpu.usage_pct": 7.5,
            "pwr.12v.voltage_v": {"min": 11.8, "max": 12.3, "avg": 12.1},
        },
    )
    stored = _stored(station)
    assert stored["cpu.usage_pct"]["min"] == 7.5
    assert stored["cpu.usage_pct"]["max"] == 7.5
    assert stored["cpu.usage_pct"]["avg"] == 7.5
    assert stored["pwr.12v.voltage_v"] == {
        "min": 11.8,
        "max": 12.3,
        "avg": 12.1,
        "ts": stored["pwr.12v.voltage_v"]["ts"],
    }
    assert stored["cpu.usage_pct"]["ts"]


def test_invalid_keys_and_values_dropped_others_kept(station):
    _ingest(
        station,
        metrics={
            "a.string": "x",
            "a.bool": True,
            "a.nan": math.nan,
            "a.inf": math.inf,
            "a.partial": {"min": 1},
            "a.nested": {"min": {"x": 1}, "max": 2, "avg": 1},
            "Bad.Key": 1,
            "nodot": 1,
            "ok.value": 3,
        },
    )
    assert set(_stored(station)) == {"ok.value"}


def test_more_than_max_keys_truncated(station):
    metrics = {f"x.k{i:04d}": float(i) for i in range(MAX_KEYS + 50)}
    _ingest(station, metrics=metrics)
    assert len(_stored(station)) == MAX_KEYS


def test_non_dict_metrics_ignored(station):
    _ingest(station, metrics="x")
    assert _stored_or_empty(station) == {}


def _stored_or_empty(station):
    tel = StationTelemetry.objects.filter(station=station).first()
    return tel.metrics if tel else {}


def test_metrics_replaced_not_merged(station):
    _ingest(station, metrics={"a.one": 1, "a.two": 2})
    _ingest(station, metrics={"a.two": 5})
    stored = _stored(station)
    assert set(stored) == {"a.two"}
    assert stored["a.two"]["avg"] == 5


def test_blocked_key_dropped(station):
    _define("a.blocked", is_blocked=True)
    _ingest(station, metrics={"a.blocked": 1, "a.ok": 2})
    assert set(_stored(station)) == {"a.ok"}


def test_out_of_agent_range_dropped_and_rejection_recorded(station):
    _define("pwr.12v.voltage_v", min_value=0, max_value=30)
    _ingest(station, metrics={"pwr.12v.voltage_v": {"min": 10, "max": 35, "avg": 12}})
    assert "pwr.12v.voltage_v" not in _stored_or_empty(station)
    d = MetricDefinition.objects.get(key="pwr.12v.voltage_v")
    assert d.last_rejected_at is not None
    assert d.last_rejected_value == {"min": 10.0, "max": 35.0, "avg": 12.0}


def test_override_range_wins_over_agent_range(station):
    _define("pwr.12v.voltage_v", min_value=0, max_value=30, max_override=40)
    _ingest(station, metrics={"pwr.12v.voltage_v": 35})
    assert _stored(station)["pwr.12v.voltage_v"]["avg"] == 35
    assert MetricDefinition.objects.get(key="pwr.12v.voltage_v").last_rejected_at is None


def test_override_min_tighter_than_agent_rejects(station):
    _define("a.val", min_value=0, min_override=5)
    _ingest(station, metrics={"a.val": 3})
    assert "a.val" not in _stored_or_empty(station)


def test_negative_current_kept_when_range_allows(station):
    _define("pwr.12v.current_a", min_value=-8.2, max_value=8.2)
    _ingest(station, metrics={"pwr.12v.current_a": {"min": -1.5, "max": 0.2, "avg": -0.4}})
    assert _stored(station)["pwr.12v.current_a"]["min"] == -1.5


def test_last_seen_updated_for_defined_keys(station):
    d = _define("a.val")
    old = d.last_seen
    _ingest(station, metrics={"a.val": 1})
    d.refresh_from_db()
    assert d.last_seen > old


def test_key_without_definition_stored_and_required(station):
    required = _ingest(station, metrics={"new.thing": 1})
    assert _stored(station)["new.thing"]["avg"] == 1
    assert required is True
    assert not MetricDefinition.objects.filter(key="new.thing").exists()


# --- descriptors --------------------------------------------------------------


def test_descriptors_create_rows(station):
    descs = [_desc("pwr.12v.voltage_v", label="12 V rail voltage", min=0, max=30)]
    required = _ingest(station, metrics={"pwr.12v.voltage_v": 12.1}, descriptors=descs)
    d = MetricDefinition.objects.get(key="pwr.12v.voltage_v")
    assert d.label == "12 V rail voltage"
    assert d.unit == "V"
    assert d.group == "power"
    assert d.precision == 2
    assert d.min_value == 0
    assert d.max_value == 30
    assert d.source_agent_version == "0.1.0"
    assert required is False
    assert _stored(station)["pwr.12v.voltage_v"]["avg"] == 12.1


def test_new_descriptor_range_applied_same_heartbeat(station):
    descs = [_desc("pwr.12v.voltage_v", min=0, max=30)]
    _ingest(station, metrics={"pwr.12v.voltage_v": 99}, descriptors=descs)
    assert "pwr.12v.voltage_v" not in _stored_or_empty(station)
    assert MetricDefinition.objects.get(key="pwr.12v.voltage_v").last_rejected_at is not None


def test_invalid_descriptors_dropped_and_deduped_last_wins(station):
    descs = [
        "garbage",
        {"key": "BAD"},
        _desc("a.val", label="first"),
        _desc("a.val", label="second", group="nonsense"),
    ]
    _ingest(station, descriptors=descs)
    assert MetricDefinition.objects.filter(key__in=["BAD"]).count() == 0
    d = MetricDefinition.objects.get(key="a.val")
    assert d.label == "second"
    assert d.group == "other"


def test_last_writer_wins_override_untouched(station_factory):
    s1, s2 = station_factory(), station_factory()
    _ingest(s1, descriptors=[_desc("a.val", label="Old label", min=0, max=10)])
    MetricDefinition.objects.filter(key="a.val").update(
        label_override="Mein Label", min_override=-5, is_hidden=True, is_blocked=False
    )
    _ingest(
        s2, descriptors=[_desc("a.val", label="New label", min=1, max=20)], agent_version="0.2.0"
    )
    d = MetricDefinition.objects.get(key="a.val")
    assert d.label == "New label"
    assert d.min_value == 1
    assert d.max_value == 20
    assert d.source_agent_version == "0.2.0"
    assert d.label_override == "Mein Label"
    assert d.min_override == -5
    assert d.is_hidden is True


def test_cap_prevents_new_definitions_and_drops_values(station, caplog):
    existing = MetricDefinition.objects.count()
    MetricDefinition.objects.bulk_create(
        [MetricDefinition(key=f"fill.k{i:05d}") for i in range(MAX_DEFINITIONS - existing)]
    )
    assert MetricDefinition.objects.count() == MAX_DEFINITIONS
    required = _ingest(
        station, metrics={"over.cap": 1, "fill.k00001": 2}, descriptors=[_desc("over.cap")]
    )
    assert not MetricDefinition.objects.filter(key="over.cap").exists()
    stored = _stored(station)
    assert "over.cap" not in stored
    assert stored["fill.k00001"]["avg"] == 2
    assert required is False
    assert "over.cap" in caplog.text
    # Rate-limited: a second heartbeat does not log again for the same key.
    caplog.clear()
    _ingest(station, metrics={"over.cap": 1})
    assert "over.cap" not in caplog.text


def test_cap_without_list_not_required(station):
    existing = MetricDefinition.objects.count()
    MetricDefinition.objects.bulk_create(
        [MetricDefinition(key=f"fill.k{i:05d}") for i in range(MAX_DEFINITIONS - existing)]
    )
    assert _ingest(station, metrics={"over.cap": 1}) is False


def test_stored_hash_equals_contract_hash(station):
    descs = [_desc("b.val", min=0, max=1), _desc("a.val")]
    _ingest(station, descriptors=descs, descriptors_hash="whatever")
    expected = descriptors_hash([clean_descriptor(d) for d in descs])
    assert StationTelemetry.objects.get(station=station).metric_descriptors_hash == expected


def test_mismatched_hash_requires_descriptors(station):
    descs = [_desc("a.val")]
    _ingest(station, metrics={"a.val": 1}, descriptors=descs)
    assert _ingest(station, metrics={"a.val": 1}, descriptors_hash="0" * 64) is True


def test_unknown_hash_without_stored_requires(station):
    _define("a.val")
    assert _ingest(station, metrics={"a.val": 1}, descriptors_hash="f" * 64) is True


def test_same_hash_not_required(station):
    descs = [_desc("a.val")]
    h = descriptors_hash([clean_descriptor(d) for d in descs])
    _ingest(station, metrics={"a.val": 1}, descriptors=descs, descriptors_hash=h)
    assert _ingest(station, metrics={"a.val": 1}, descriptors_hash=h) is False


def test_list_sent_with_unknown_value_key_not_required(station):
    """Review Focus 1: agent sent its list but a value key lacks a descriptor."""
    descs = [_desc("a.val")]
    h = descriptors_hash([clean_descriptor(d) for d in descs])
    required = _ingest(
        station,
        metrics={"a.val": 1, "undescribed.key": 2},
        descriptors=descs,
        descriptors_hash="mismatch",
    )
    assert required is False
    assert _stored(station)["undescribed.key"]["avg"] == 2
    assert StationTelemetry.objects.get(station=station).metric_descriptors_hash == h


def test_no_hash_no_list_no_unknown_keys_not_required(station):
    _define("a.val")
    assert _ingest(station, metrics={"a.val": 1}) is False


# --- legacy fallback ----------------------------------------------------------

LEGACY_INVENTORY = {
    "cpu": {"temperature_c": 51.5},
    "ram": {"usage_percent": 42.0},
    "disk": [
        {"mount": "/", "usage_percent": 61.0},
        {"mount": "/boot", "usage_percent": 23.0},
        {"mount": "/data", "usage_percent": "garbage"},
        "garbage",
    ],
}


def test_legacy_fallback_from_inventory(station):
    required = _ingest(
        station,
        inventory=LEGACY_INVENTORY,
        telemetry={"boot": {"uptime_seconds": 1234.0}},
    )
    stored = _stored(station)
    assert stored["cpu.temp_c"]["avg"] == 51.5
    assert stored["mem.used_pct"]["avg"] == 42.0
    assert stored["disk.root.used_pct"]["avg"] == 61.0
    assert stored["disk.boot.used_pct"]["avg"] == 23.0
    assert stored["sys.uptime_s"]["avg"] == 1234.0
    assert "disk.data.used_pct" not in stored
    # Legacy definitions are seeded by data migration -> nothing to request.
    assert required is False


def test_legacy_fallback_ignores_bools_and_garbage(station):
    _ingest(
        station,
        inventory={"cpu": {"temperature_c": True}, "ram": "x", "disk": "x"},
        telemetry="garbage",
    )
    assert _stored_or_empty(station) == {}


def test_legacy_fallback_does_not_override_explicit_metrics(station):
    _ingest(station, metrics={"cpu.temp_c": 40.0}, inventory=LEGACY_INVENTORY)
    stored = _stored(station)
    assert stored["cpu.temp_c"]["avg"] == 40.0
    assert stored["mem.used_pct"]["avg"] == 42.0


def test_nothing_to_store_creates_no_telemetry_row(station):
    _ingest(station, metrics=None, inventory={})
    assert not StationTelemetry.objects.filter(station=station).exists()


def test_query_count_bounded(station, django_assert_max_num_queries):
    descs = [_desc(f"a.k{i:03d}") for i in range(50)]
    _ingest(station, metrics={f"a.k{i:03d}": i for i in range(50)}, descriptors=descs)
    with django_assert_max_num_queries(8):
        _ingest(
            station,
            metrics={f"a.k{i:03d}": i for i in range(50)},
            descriptors=descs,
            inventory=LEGACY_INVENTORY,
        )
    with django_assert_max_num_queries(5):
        _ingest(station, metrics={f"a.k{i:03d}": i for i in range(50)}, descriptors_hash="x")
