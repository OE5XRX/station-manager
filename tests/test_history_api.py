"""History API: ``GET /api/v1/stations/{id}/metrics/history/`` (SQLite + Postgres).

Data timestamps are relative to the real ``now`` (the view uses ``timezone.now()``); on
Postgres the months they fall into are created up front (``_partitions``).
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.history import query
from apps.history.models import MetricHourly, MetricSample
from apps.history.partitions import PARTITIONED_TABLES, add_months, ensure_partitions, month_start
from apps.monitoring.models import MetricDefinition
from apps.stations.models import StationTelemetry
from tests.test_api_read_fixtures import anon_client, bearer, client_for, topology  # noqa: F401

pytestmark = pytest.mark.django_db

KEY = "pwr.12v.voltage_v"
KEY2 = "sys.cpu.temp_c"
HIDDEN = "sys.mem.used_pct"


def _url(pk):
    return f"/api/v1/stations/{pk}/metrics/history/"


def _iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _partitions(db):
    # Cover everything the tests write: up to ~400 days back and the next months (no-op on SQLite).
    first = add_months(month_start(timezone.now()), -15)
    for table in PARTITIONED_TABLES:
        ensure_partitions(connection, table, first, 18)


@pytest.fixture
def hour():
    """A fully closed UTC hour start, two hours ago."""
    now = timezone.now().astimezone(UTC)
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)


@pytest.fixture
def defs(db):
    MetricDefinition.objects.create(
        key=KEY, label="12 V rail", unit="V", group="power", precision=2
    )
    MetricDefinition.objects.create(key=KEY2, label="CPU", unit="°C", group="system")
    MetricDefinition.objects.create(key=HIDDEN, label="Mem", unit="%", is_hidden=True)


def _sample(station, key, ts, v_min, v_avg, v_max):
    MetricSample.objects.create(
        station=station, metric_key=key, ts=ts, min=v_min, avg=v_avg, max=v_max
    )


def _hourly(station, key, ts, v_min, v_avg, v_max, count):
    MetricHourly.objects.create(
        station=station, metric_key=key, ts=ts, min=v_min, avg=v_avg, max=v_max, count=count
    )


# --- permissions: identical to /telemetry/ ---------------------------------------------


@pytest.mark.parametrize("role", ["admin", "staff", "region_mgr", "station_user", "applicant"])
@pytest.mark.parametrize("station", ["station_in", "station_out"])
def test_permission_matrix_equals_telemetry(topology, role, station):  # noqa: F811
    s = topology[station]
    StationTelemetry.objects.create(station=s, boot_count=1)
    client = client_for(topology[role])
    tel = client.get(f"/api/v1/stations/{s.pk}/telemetry/")
    hist = client.get(_url(s.pk))
    assert tel.status_code in (200, 401, 403)
    assert hist.status_code == tel.status_code


def test_anonymous_same_as_telemetry(topology):  # noqa: F811
    s = topology["station_in"]
    StationTelemetry.objects.create(station=s, boot_count=1)
    c = anon_client()
    tel = c.get(f"/api/v1/stations/{s.pk}/telemetry/")
    hist = c.get(_url(s.pk))
    assert tel.status_code in (401, 403)
    assert hist.status_code == tel.status_code


def test_unknown_station_404(topology):  # noqa: F811
    assert bearer(topology["admin"]).get(_url(999999)).status_code == 404


def test_no_telemetry_row_is_200_empty(topology):  # noqa: F811
    # History has no "missing row" case: a station without telemetry just has no series.
    resp = bearer(topology["station_user"]).get(_url(topology["station_in"].pk))
    assert resp.status_code == 200
    assert resp.data["series"] == []


# --- choose_resolution -----------------------------------------------------------------

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        # raw is estimated at the worst-case 10 s heartbeat: 1 h = 360 points.
        (timedelta(hours=1), ("raw", "raw")),
        (timedelta(hours=2), ("raw", "raw")),
        (timedelta(hours=3), ("5m", "raw")),  # 1080 raw points > MAX_POINTS
        (timedelta(hours=24), ("5m", "raw")),
        (timedelta(days=7), ("1h", "hourly")),
        (timedelta(days=365), ("1d", "hourly")),
        (timedelta(days=730), ("1d", "hourly")),
    ],
)
def test_choose_resolution_auto(span, expected):
    assert query.choose_resolution("auto", NOW - span, NOW, NOW) == expected


def test_choose_resolution_fine_before_raw_window_falls_back_to_hourly():
    since = NOW - timedelta(days=40)
    assert query.choose_resolution("5m", since, since + timedelta(hours=6), NOW) == (
        "1h",
        "hourly",
    )
    assert query.choose_resolution("raw", since, since + timedelta(hours=1), NOW) == (
        "1h",
        "hourly",
    )


def test_choose_resolution_explicit():
    since = NOW - timedelta(hours=3)
    assert query.choose_resolution("raw", since, NOW, NOW) == ("raw", "raw")
    assert query.choose_resolution("5m", since, NOW, NOW) == ("5m", "raw")
    assert query.choose_resolution("1h", since, NOW, NOW) == ("1h", "hourly")
    assert query.choose_resolution("1d", since, NOW, NOW) == ("1d", "hourly")


# --- data ------------------------------------------------------------------------------


def test_5m_buckets_from_raw(topology, defs, hour):  # noqa: F811
    s = topology["station_in"]
    _sample(s, KEY, hour + timedelta(minutes=1), 11.0, 12.0, 13.0)
    _sample(s, KEY, hour + timedelta(minutes=2), 11.5, 14.0, 15.0)
    _sample(s, KEY, hour + timedelta(minutes=6), 10.0, 10.5, 11.0)
    resp = bearer(topology["station_user"]).get(
        _url(s.pk),
        {
            "keys": KEY,
            "since": _iso(hour),
            "until": _iso(hour + timedelta(hours=1)),
            "resolution": "5m",
        },
    )
    assert resp.status_code == 200, resp.data
    body = resp.json()
    assert body["resolution"] == "5m" and body["source"] == "raw"
    assert body["station"] == s.pk
    assert body["since"] == _iso(hour) and body["until"] == _iso(hour + timedelta(hours=1))
    (series,) = body["series"]
    assert series == {
        "key": KEY,
        "label": "12 V rail",
        "unit": "V",
        "precision": 2,
        "group": "power",
        "points": [
            [int(hour.timestamp()), 11.0, 13.0, 15.0],
            [int(hour.timestamp()) + 300, 10.0, 10.5, 11.0],
        ],
    }
    assert all(isinstance(p[0], int) for p in series["points"])


def test_raw_rows_as_is(topology, defs, hour):  # noqa: F811
    s = topology["station_in"]
    _sample(s, KEY, hour + timedelta(minutes=2), 2.0, 2.5, 3.0)
    _sample(s, KEY, hour + timedelta(minutes=1), 1.0, 1.5, 2.0)
    _sample(topology["station_out"], KEY, hour + timedelta(minutes=1), 9.0, 9.0, 9.0)
    resp = bearer(topology["admin"]).get(
        _url(s.pk),
        {"keys": KEY, "since": _iso(hour), "until": _iso(hour + timedelta(minutes=30))},
    )
    body = resp.json()
    assert body["resolution"] == "raw" and body["source"] == "raw"
    assert body["series"][0]["points"] == [
        [int(hour.timestamp()) + 60, 1.0, 1.5, 2.0],
        [int(hour.timestamp()) + 120, 2.0, 2.5, 3.0],
    ]


def test_1h_from_hourly(topology, defs, hour):  # noqa: F811
    s = topology["station_in"]
    h0 = hour - timedelta(hours=5)
    _hourly(s, KEY, h0, 11.0, 12.0, 13.0, 60)
    _hourly(s, KEY, h0 + timedelta(hours=1), 10.0, 11.0, 12.0, 58)
    _sample(s, KEY, h0 + timedelta(minutes=3), 1.0, 1.0, 1.0)  # raw must not be used
    resp = bearer(topology["admin"]).get(
        _url(s.pk),
        {"keys": KEY, "since": _iso(h0), "until": _iso(hour), "resolution": "1h"},
    )
    body = resp.json()
    assert body["resolution"] == "1h" and body["source"] == "hourly"
    assert body["series"][0]["points"] == [
        [int(h0.timestamp()), 11.0, 12.0, 13.0],
        [int(h0.timestamp()) + 3600, 10.0, 11.0, 12.0],
    ]


def test_1d_weighted_average_utc_day(topology, defs):  # noqa: F811
    s = topology["station_in"]
    day = (timezone.now().astimezone(UTC) - timedelta(days=3)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # 23:00 UTC is already the next day in Europe/Vienna — must still count for `day`.
    _hourly(s, KEY, day + timedelta(hours=1), 5.0, 10.0, 12.0, 1)
    _hourly(s, KEY, day + timedelta(hours=23), 8.0, 20.0, 30.0, 3)
    _hourly(s, KEY, day + timedelta(days=1, hours=2), 1.0, 2.0, 3.0, 2)
    resp = bearer(topology["admin"]).get(
        _url(s.pk),
        {
            "keys": KEY,
            "since": _iso(day),
            "until": _iso(day + timedelta(days=2)),
            "resolution": "1d",
        },
    )
    body = resp.json()
    assert body["resolution"] == "1d" and body["source"] == "hourly"
    points = body["series"][0]["points"]
    assert points[0][0] == int(day.timestamp())
    assert points[0][1:] == [5.0, pytest.approx(17.5), 30.0]
    assert points[1] == [int(day.timestamp()) + 86400, 1.0, 2.0, 3.0]
    assert len(points) == 2


def test_fine_resolution_before_raw_window_served_from_hourly(topology, defs):  # noqa: F811
    # Review focus 4: since older than the raw window + resolution=5m → hourly, not empty.
    s = topology["station_in"]
    h0 = (timezone.now().astimezone(UTC) - timedelta(days=40)).replace(
        minute=0, second=0, microsecond=0
    )
    _hourly(s, KEY, h0, 11.0, 12.0, 13.0, 60)
    resp = bearer(topology["admin"]).get(
        _url(s.pk),
        {
            "keys": KEY,
            "since": _iso(h0),
            "until": _iso(h0 + timedelta(hours=6)),
            "resolution": "5m",
        },
    )
    assert resp.status_code == 200, resp.data
    body = resp.json()
    assert body["resolution"] == "1h" and body["source"] == "hourly"
    assert body["series"][0]["points"] == [[int(h0.timestamp()), 11.0, 12.0, 13.0]]


def test_defaults_last_24h_auto(topology, defs):  # noqa: F811
    s = topology["station_in"]
    StationTelemetry.objects.create(station=s, metrics={KEY: {"min": 1, "max": 1, "avg": 1}})
    resp = bearer(topology["admin"]).get(_url(s.pk))
    body = resp.json()
    assert body["resolution"] == "5m" and body["source"] == "raw"
    since = datetime.fromisoformat(body["since"].replace("Z", "+00:00"))
    until = datetime.fromisoformat(body["until"].replace("Z", "+00:00"))
    assert body["since"].endswith("Z") and body["until"].endswith("Z")
    assert until - since == timedelta(hours=24)
    assert abs((timezone.now() - until).total_seconds()) < 60


def test_naive_dates_are_utc(topology, defs, hour):  # noqa: F811
    s = topology["station_in"]
    naive = hour.replace(tzinfo=None).isoformat()
    resp = bearer(topology["admin"]).get(
        _url(s.pk), {"keys": KEY, "since": naive, "until": _iso(hour + timedelta(hours=1))}
    )
    assert resp.status_code == 200, resp.data
    assert resp.json()["since"] == _iso(hour)


def test_default_keys_exclude_hidden_and_undefined(topology, defs):  # noqa: F811
    s = topology["station_in"]
    MetricDefinition.objects.create(key="sys.disk.blk_pct", label="Blk", is_blocked=True)
    v = {"min": 1, "max": 1, "avg": 1}
    StationTelemetry.objects.create(
        station=s,
        metrics={KEY: v, KEY2: v, HIDDEN: v, "sys.disk.blk_pct": v, "x.no.def": v},
    )
    resp = bearer(topology["admin"]).get(_url(s.pk))
    assert sorted(x["key"] for x in resp.json()["series"]) == sorted([KEY, KEY2])


def test_explicit_undefined_keys_included(topology, defs, hour):  # noqa: F811
    s = topology["station_in"]
    _sample(s, KEY, hour + timedelta(minutes=1), 1.0, 2.0, 3.0)
    resp = bearer(topology["admin"]).get(
        _url(s.pk),
        {
            "keys": f"{KEY},x.no.def",
            "since": _iso(hour),
            "until": _iso(hour + timedelta(hours=1)),
        },
    )
    assert resp.status_code == 200, resp.data
    series = resp.json()["series"]
    assert [x["key"] for x in series] == [KEY, "x.no.def"]
    assert series[0]["label"] == "12 V rail" and len(series[0]["points"]) == 1
    assert series[1] == {
        "key": "x.no.def",
        "label": "x.no.def",
        "unit": "",
        "precision": 1,
        "group": "other",
        "points": [],
    }


def test_explicit_hidden_and_blocked_keys_dropped(topology, defs, hour):  # noqa: F811
    # Parity with /telemetry/, which never shows hidden (or blocked) metrics.
    s = topology["station_in"]
    blocked = "sys.disk.blk_pct"
    MetricDefinition.objects.create(key=blocked, label="Blk", is_blocked=True)
    _sample(s, HIDDEN, hour + timedelta(minutes=1), 1.0, 2.0, 3.0)
    _sample(s, blocked, hour + timedelta(minutes=1), 1.0, 2.0, 3.0)
    _sample(s, KEY, hour + timedelta(minutes=1), 1.0, 2.0, 3.0)
    resp = bearer(topology["admin"]).get(
        _url(s.pk),
        {
            "keys": f"{HIDDEN},{blocked},{KEY}",
            "since": _iso(hour),
            "until": _iso(hour + timedelta(hours=1)),
        },
    )
    assert resp.status_code == 200, resp.data
    assert [x["key"] for x in resp.json()["series"]] == [KEY]


def test_only_hidden_key_requested_returns_no_series(topology, defs, hour):  # noqa: F811
    s = topology["station_in"]
    _sample(s, HIDDEN, hour + timedelta(minutes=1), 1.0, 2.0, 3.0)
    resp = bearer(topology["admin"]).get(
        _url(s.pk),
        {"keys": HIDDEN, "since": _iso(hour), "until": _iso(hour + timedelta(hours=1))},
    )
    assert resp.status_code == 200, resp.data
    assert resp.json()["series"] == []


def test_5m_bucketing_in_db_bounds_rows(topology, defs, hour):  # noqa: F811
    # 10 s heartbeats: 30 raw rows per bucket collapse to one point per 5 min in SQL.
    s = topology["station_in"]
    rows = [
        MetricSample(
            station=s,
            metric_key=KEY,
            ts=hour + timedelta(seconds=10 * i),
            min=float(i),
            avg=float(i) + 0.5,
            max=float(i) + 1,
        )
        for i in range(60)
    ]
    MetricSample.objects.bulk_create(rows)
    points = query.fetch_series(s, [KEY], hour, hour + timedelta(minutes=10), "5m", "raw")[KEY]
    t0 = int(hour.timestamp())
    assert points == [
        [t0, 0.0, pytest.approx(15.0), 30.0],
        [t0 + 300, 30.0, pytest.approx(45.0), 60.0],
    ]
    assert all(type(p[0]) is int for p in points)


def test_5m_bucket_floor_is_utc_epoch_aligned(topology, defs, hour):  # noqa: F811
    s = topology["station_in"]
    _sample(s, KEY, hour + timedelta(minutes=4, seconds=59, microseconds=999000), 1, 1, 1)
    _sample(s, KEY, hour + timedelta(minutes=5), 2, 2, 2)
    # since inside the first bucket: the lower bound is floored, the bucket stays whole.
    points = query.fetch_series(
        s, [KEY], hour + timedelta(minutes=2), hour + timedelta(minutes=10), "5m", "raw"
    )[KEY]
    t0 = int(hour.timestamp())
    assert points == [[t0, 1.0, 1.0, 1.0], [t0 + 300, 2.0, 2.0, 2.0]]


# --- limits / validation ---------------------------------------------------------------


def _get(topology, params):  # noqa: F811
    return bearer(topology["admin"]).get(_url(topology["station_in"].pk), params)


def test_too_many_keys_400(topology):  # noqa: F811
    keys = ",".join(f"a.b.k{i}" for i in range(query.MAX_KEYS + 1))
    resp = _get(topology, {"keys": keys})
    assert resp.status_code == 400 and "keys" in resp.data


def test_max_keys_ok(topology):  # noqa: F811
    keys = ",".join(f"a.b.k{i}" for i in range(query.MAX_KEYS))
    assert _get(topology, {"keys": keys}).status_code == 200


def test_invalid_key_400(topology):  # noqa: F811
    resp = _get(topology, {"keys": f"{KEY},Bad Key"})
    assert resp.status_code == 400 and "keys" in resp.data


def test_span_too_long_400(topology):  # noqa: F811
    now = timezone.now()
    resp = _get(topology, {"since": _iso(now - timedelta(days=800)), "until": _iso(now)})
    assert resp.status_code == 400 and "since" in resp.data


def test_raw_over_24h_exceeds_hard_max_400(topology):  # noqa: F811
    # raw is budgeted at a 10 s heartbeat: 24 h = 8640 points > HARD_MAX_POINTS.
    now = timezone.now()
    resp = _get(
        topology,
        {"since": _iso(now - timedelta(hours=24)), "until": _iso(now), "resolution": "raw"},
    )
    assert resp.status_code == 400 and "resolution" in resp.data


def test_max_span_follows_setting_per_call(topology, settings):  # noqa: F811
    settings.HISTORY_HOURLY_RETENTION_DAYS = 10
    assert query.max_span() == timedelta(days=10)
    now = timezone.now()
    resp = _get(topology, {"since": _iso(now - timedelta(days=20)), "until": _iso(now)})
    assert resp.status_code == 400 and "since" in resp.data
    assert "10 days" in str(resp.data["since"])
    ok = _get(topology, {"since": _iso(now - timedelta(days=9)), "until": _iso(now)})
    assert ok.status_code == 200, ok.data


def test_raw_over_7d_exceeds_hard_max_400(topology):  # noqa: F811
    now = timezone.now()
    resp = _get(
        topology,
        {"since": _iso(now - timedelta(days=7)), "until": _iso(now), "resolution": "raw"},
    )
    assert resp.status_code == 400 and "resolution" in resp.data


def test_since_not_before_until_400(topology):  # noqa: F811
    now = timezone.now()
    resp = _get(topology, {"since": _iso(now), "until": _iso(now)})
    assert resp.status_code == 400 and "since" in resp.data


def test_garbage_date_400(topology):  # noqa: F811
    resp = _get(topology, {"since": "yesterday-ish"})
    assert resp.status_code == 400 and "since" in resp.data


def test_unknown_resolution_400(topology):  # noqa: F811
    resp = _get(topology, {"resolution": "10s"})
    assert resp.status_code == 400 and "resolution" in resp.data


# --- schema ----------------------------------------------------------------------------


def test_schema_documents_history_endpoint(topology):  # noqa: F811
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    op = resp.data["paths"]["/api/v1/stations/{id}/metrics/history/"]["get"]
    names = {p["name"] for p in op["parameters"]}
    assert {"keys", "since", "until", "resolution"} <= names
    assert "1000" in op["description"] and "5000" in op["description"]
