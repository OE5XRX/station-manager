"""Station detail: metric history block in the Metrics tab (history Task 5).

Markup only — the history API is exercised in tests/test_history_api.py and
the chart logic in tests/js/metrics-history-logic.test.mjs.
"""

import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.monitoring.models import MetricDefinition
from apps.stations.models import Station, StationTelemetry

REPO_ROOT = Path(__file__).resolve().parent.parent


def _setup(with_metrics=True):
    rows = [
        ("pwr.12v.voltage_v", "power", False),
        ("pwr.12v.current_a", "power", False),
        ("cpu.temp_c", "system", False),
        ("pwr.secret_v", "power", True),
    ]
    for key, group, hidden in rows:
        MetricDefinition.objects.update_or_create(
            key=key, defaults={"label": key, "unit": "V", "group": group, "is_hidden": hidden}
        )
    station = Station.objects.create(name="OE5H")
    metrics = {}
    if with_metrics:
        metrics = {k: {"min": 1.0, "max": 2.0, "avg": 1.5, "ts": "t"} for k, _g, _h in rows}
    StationTelemetry.objects.create(station=station, metrics=metrics)
    return station


def _get(client, admin_user, station):
    client.force_login(admin_user)
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert resp.status_code == 200
    return resp.content.decode()


def _blocks(content):
    return re.findall(r"<details class=\"mx-hist\"(.*?)</details>", content, re.S)


@pytest.mark.django_db
def test_history_block_per_group(client, admin_user):
    station = _setup()
    content = _get(client, admin_user, station)
    blocks = _blocks(content)
    assert len(blocks) == 2  # system + power
    assert content.count(f'data-history-station="{station.pk}"') == 2
    keys = re.findall(r'data-history-keys="([^"]*)"', content)
    assert "cpu.temp_c" in keys
    assert "pwr.12v.current_a,pwr.12v.voltage_v" in keys
    for block in blocks:
        for rng in ("1h", "24h", "7d", "30d", "1y"):
            assert f'data-history-range="{rng}"' in block
        assert "data-history-charts" in block
        assert "data-history-status" in block
        assert 'role="status"' in block
    # 24h is the preselected range.
    assert re.search(
        r'class="btn btn-sm active" data-history-range="24h" aria-pressed="true"', content
    )
    assert "<details" in content and "<summary" in content


@pytest.mark.django_db
def test_hidden_metric_not_in_history_keys(client, admin_user):
    station = _setup()
    content = _get(client, admin_user, station)
    for keys in re.findall(r'data-history-keys="([^"]*)"', content):
        assert "pwr.secret_v" not in keys
    assert "pwr.secret_v" not in content


@pytest.mark.django_db
def test_history_assets_local_with_nonce(client, admin_user):
    station = _setup()
    content = _get(client, admin_user, station)
    for asset in (
        "vendor/uplot/uPlot.iife.min.js",
        "js/metrics-history-logic.js",
        "js/metrics-history.js",
    ):
        m = re.search(r"<script[^>]*src=\"[^\"]*" + re.escape(asset) + r"\"[^>]*>", content)
        assert m, asset
        assert 'nonce="' in m.group(0), asset
    css = re.search(r"<link[^>]*vendor/uplot/uPlot\.min\.css[^>]*>", content)
    assert css and 'rel="stylesheet"' in css.group(0) and 'nonce="' in css.group(0)
    # Logic module loads before the glue that consumes it.
    assert content.index("metrics-history-logic.js") < content.index("js/metrics-history.js")
    assert "cdn.jsdelivr.net/npm/uplot" not in content
    assert "unpkg.com" not in content


@pytest.mark.django_db
def test_no_history_block_without_metrics(client, admin_user):
    station = _setup(with_metrics=False)
    content = _get(client, admin_user, station)
    assert "data-history-station" not in content
    assert "uPlot.iife.min.js" not in content
    assert "metrics-history.js" not in content
    assert "No metrics reported yet" in content


@pytest.mark.django_db
def test_no_history_block_when_only_hidden_metrics(client, admin_user):
    MetricDefinition.objects.create(key="pwr.secret_v", label="s", group="power", is_hidden=True)
    station = Station.objects.create(name="OE5Q")
    StationTelemetry.objects.create(
        station=station, metrics={"pwr.secret_v": {"min": 1, "max": 1, "avg": 1, "ts": "t"}}
    )
    content = _get(client, admin_user, station)
    assert "data-history-station" not in content


def test_static_assets_exist():
    for rel in (
        "static/vendor/uplot/uPlot.iife.min.js",
        "static/vendor/uplot/uPlot.min.css",
        "static/vendor/uplot/LICENSE",
        "static/js/metrics-history-logic.js",
        "static/js/metrics-history.js",
    ):
        assert (REPO_ROOT / rel).is_file(), rel
    head = (REPO_ROOT / "static/vendor/uplot/uPlot.iife.min.js").read_text()[:200]
    assert "v1.6.32" in head
    assert "MIT" in (REPO_ROOT / "static/vendor/uplot/LICENSE").read_text()


def test_history_js_never_uses_locale_number_formatting():
    for rel in ("static/js/metrics-history-logic.js", "static/js/metrics-history.js"):
        src = (REPO_ROOT / rel).read_text()
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        assert "toLocaleString" not in code, rel
        assert "innerHTML" not in code, rel


def test_js_chunk_size_matches_api_key_cap():
    # A metric group can hold more keys than the API accepts per request; the
    # browser glue chunks by MAX_KEYS_PER_REQUEST, which must track the server cap.
    from apps.history.query import MAX_KEYS

    logic = (REPO_ROOT / "static/js/metrics-history-logic.js").read_text()
    m = re.search(r"var MAX_KEYS_PER_REQUEST = (\d+);", logic)
    assert m and int(m.group(1)) == MAX_KEYS
    glue = (REPO_ROOT / "static/js/metrics-history.js").read_text()
    assert "L.chunkKeys(" in glue and "L.mergeResponses(" in glue
