# Station Telemetry History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every accepted generic metric per heartbeat (raw min/max/avg 30 d, hourly min/max/avg/count 2 y), expose it via `/api/v1/stations/{id}/metrics/history/` and chart it in the station "Metrics" tab.

**Architecture:** New app `apps/history` with two composite-PK tables (`MetricSample`, `MetricHourly`) that are monthly RANGE-partitioned on Postgres (hand-written vendor-switched migration; plain tables on SQLite). Ingest hooks into `ingest_metrics` (savepoint + error isolation), a `history_maintain --loop` command compacts closed hours (watermark in `HistoryState`), creates partitions and drops expired ones. A read-only API action on `StationViewSet` serves server-side downsampled series; the UI lazy-loads them into vendored uPlot charts.

**Tech Stack:** Django 6.0 (`CompositePrimaryKey`), PostgreSQL 18 declarative partitioning, DRF + drf-spectacular, uPlot 1.6.32 (MIT, vendored static asset), vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-05-station-telemetry-history-design.md`

## Global Constraints

- Retention: `HISTORY_RAW_RETENTION_DAYS = 30`, `HISTORY_HOURLY_RETENTION_DAYS = 730` (settings, base.py).
- Raw partition is dropped only when its *end* is older than 30 days (and fully compacted); hourly partition when its end is older than 730 days. No row-wise deletes on Postgres. No DEFAULT partition.
- Migration creates partitions for the current month + the next **3** months (UTC months).
- History errors never break the heartbeat (savepoint + log; missing partition logs once per hour via `cache.add`).
- Only accepted values are stored: definition exists, not `is_blocked`, inside the plausibility range. Legacy (inventory-derived) values included. Hidden metrics stored, not shown in UI.
- API permissions exactly like `/stations/{id}/telemetry/` (`StationViewSet`, area `stations` view, throttle scope `api-token`).
- No new Python dependency; uPlot vendored under `static/vendor/uplot/` (no CDN).
- Normal suite stays on SQLite; extra CI job runs `tests/test_history_*.py` on Postgres.
- Repo rules: `{% comment %}` for multi-line template comments; numbers dot-decimal in charts; tests top-level `tests/test_*.py`; ruff 0.16.8 (`ruff format` + `ruff check`); `frontend-design` skill mandatory for UI task.
- Time: all bucketing/partition boundaries in UTC (project `TIME_ZONE` is Europe/Vienna — never use local truncation).

## Review Focus

1. A heartbeat arriving when the month's partition is missing (rollout gap) → heartbeat 200, telemetry still saved, one error log per hour (Task 2 test on Postgres).
2. A heartbeat inside a request-wide transaction where the history insert fails → the outer transaction must stay usable (savepoint) (Task 2 test).
3. Maintenance run after a long outage (watermark days behind) → compacts in bounded chunks, never drops a raw partition beyond `compacted_until` (Task 3 test).
4. API with `since` older than the raw window but `resolution=5m` → served from hourly (resolution reported as `1h`), not empty (Task 4 test).
5. UI on a station without any history (new station / hidden-only metrics) → chart area shows an empty state, no JS error (Task 5 JS logic test + template test).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/history/__init__.py`, `apps.py` | App config (`HistoryConfig`, label `history`) |
| `apps/history/models.py` | `MetricSample`, `MetricHourly`, `HistoryState` |
| `apps/history/partitions.py` | Month math + Postgres partition DDL (ensure/list/drop), vendor-aware |
| `apps/history/migrations/0001_initial.py` | State = CreateModel; DB = vendor switch (partitioned on PG) |
| `apps/history/ingest.py` | `record_samples()` (error-isolated bulk insert) |
| `apps/history/maintenance.py` | `compact()`, `ensure_future_partitions()`, `drop_expired()`, `run_maintenance()` |
| `apps/history/management/commands/history_maintain.py` | CLI with `--loop --interval` |
| `apps/history/query.py` | Resolution choice + series fetch/downsampling |
| `apps/api/read_views.py` / `read_serializers.py` | `metrics_history` action + response serializers |
| `config/settings/test_postgres.py` | Test settings on Postgres (CI job) |
| `static/vendor/uplot/*`, `static/js/metrics-history-logic.js`, `static/js/metrics-history.js` | Charts |
| `apps/stations/templates/stations/_metrics_panel.html` | Collapsible history per group |

---

### Task 1: App, models, partitioned migration, Postgres CI job

**Files:**
- Create: `apps/history/{__init__,apps,models,partitions}.py`, `apps/history/migrations/{__init__,0001_initial}.py`, `config/settings/test_postgres.py`, `tests/test_history_models.py`, `tests/test_history_partitions.py`
- Modify: `config/settings/base.py` (INSTALLED_APPS += `"apps.history"`, retention settings), `.github/workflows/ci.yml` (new job)

**Interfaces:**
- Produces:
  - `MetricSample(station: FK Station CASCADE related_name="+", metric_key: CharField(max_length=MetricDefinition key max len), ts: DateTimeField, min/max/avg: FloatField)`, `pk = models.CompositePrimaryKey("station", "metric_key", "ts")`, `db_table = "history_metricsample"`.
  - `MetricHourly(...same + count: PositiveIntegerField)`, `db_table = "history_metrichourly"`, `ts` = UTC hour start.
  - `HistoryState(id=1 singleton, compacted_until: DateTimeField null)` with classmethod `get() -> HistoryState` (get_or_create pk=1).
  - `partitions.PARTITIONED_TABLES = ("history_metricsample", "history_metrichourly")`
  - `partitions.month_start(dt: datetime) -> datetime` (UTC, day 1 00:00), `partitions.add_months(dt, n) -> datetime`
  - `partitions.partition_name(table: str, month: datetime) -> str` → `f"{table}_p{YYYY}{MM}"`
  - `partitions.is_partitioned(connection) -> bool` (`connection.vendor == "postgresql"`)
  - `partitions.ensure_partitions(connection, table, first_month: datetime, count: int) -> list[str]` (created names; `CREATE TABLE IF NOT EXISTS <p> PARTITION OF <table> FOR VALUES FROM (lo) TO (hi)`; no-op `[]` on non-PG)
  - `partitions.list_partitions(connection, table) -> list[tuple[str, datetime, datetime]]` (name, lo, hi) from `pg_inherits`, only names matching `<table>_pYYYYMM`, bounds derived from the name; `[]` on non-PG.
  - `partitions.drop_partition(connection, name: str) -> None` (`DROP TABLE IF EXISTS`, quoted identifier).

- [ ] **Step 1: Write failing tests**
  - `tests/test_history_models.py` (SQLite + PG): create `MetricSample` for a station, `bulk_create` the same `(station, key, ts)` twice with `ignore_conflicts=True` → count 1; `MetricHourly.objects.bulk_create([...], update_conflicts=True, unique_fields=["station","metric_key","ts"], update_fields=["min","max","avg","count"])` twice with changed values → second values win; `HistoryState.get().compacted_until is None`; deleting the station removes its samples; `call_command("makemigrations", "history", "--check", "--dry-run")` does not raise.
  - `tests/test_history_partitions.py`: pure tests `month_start(datetime(2026,11,5,13,tzinfo=UTC)) == datetime(2026,11,1,tzinfo=UTC)`; a Vienna-tz input at `2026-12-01 00:30+01:00` → `2026-11-01 UTC`; `add_months(2026-11-01, 3) == 2027-02-01`; `partition_name("history_metricsample", 2027-02-01) == "history_metricsample_p202702"`. PG-only (fixture `require_postgres` skipping when `connection.vendor != "postgresql"`, put in `tests/conftest.py`): both tables are partitioned (`pg_partitioned_table`), `list_partitions` returns current UTC month + next 3 for each table; inserting a sample with `ts` 6 months ahead raises `IntegrityError`/`DataError`... (Postgres error "no partition of relation"); `ensure_partitions(...)` is idempotent; `drop_partition` removes it from `list_partitions`.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_history_models.py tests/test_history_partitions.py` → FAIL (app missing).

- [ ] **Step 3: Implement** app, models, partitions, settings. Migration `0001_initial`: `migrations.SeparateDatabaseAndState(state_operations=[CreateModel ×3], database_operations=[RunPython(create_tables, drop_tables)])`. `create_tables`: `HistoryState` always via `schema_editor.create_model`; for the two series tables: non-PG → `schema_editor.create_model(model)`; PG → raw SQL `CREATE TABLE <t> (station_id bigint NOT NULL REFERENCES stations_station(id) DEFERRABLE INITIALLY DEFERRED, metric_key varchar(N) NOT NULL, ts timestamptz NOT NULL, min/max/avg double precision NOT NULL[, count integer NOT NULL CHECK (count >= 0)], PRIMARY KEY (station_id, metric_key, ts)) PARTITION BY RANGE (ts)` (check the real station pk column type/table name first), then `ensure_partitions(conn, t, month_start(now), 4)`. Migration imports only `apps.history.partitions` (pure helpers, no models). Retention settings in `base.py` read from env with the defaults above.

- [ ] **Step 4: Postgres locally + CI**
  - `config/settings/test_postgres.py`: `from .test import *` then `DATABASES` = postgres from env `POSTGRES_HOST/PORT/DB/USER/PASSWORD` (defaults `localhost`, `5432`, `station_manager`).
  - CI job `test-history-postgres` ("History tests (Postgres)"): `services.postgres: image postgres:18`, env `POSTGRES_PASSWORD/USER/DB`, health-cmd `pg_isready`, ports `5432:5432`; steps checkout, setup-python 3.14, pip cache, `pip install -r requirements/dev.txt`, `pytest -q tests/test_history_*.py` with `DJANGO_SETTINGS_MODULE=config.settings.test_postgres`, `DJANGO_SECRET_KEY`, `POSTGRES_HOST=localhost`.
  - Local: `docker run -d --rm --name sm-hist-pg -e POSTGRES_PASSWORD=station_manager -e POSTGRES_USER=station_manager -e POSTGRES_DB=station_manager -p 55432:5432 postgres:18` and run with `POSTGRES_PORT=55432`.

- [ ] **Step 5: Verify** both: `.venv/bin/python -m pytest -q tests/test_history_*.py` (SQLite, PG tests skipped) and `DJANGO_SETTINGS_MODULE=config.settings.test_postgres POSTGRES_HOST=localhost POSTGRES_PORT=55432 .venv/bin/python -m pytest -q tests/test_history_*.py` → all PASS. `uvx ruff@0.16.8 format --check . && uvx ruff@0.16.8 check .` clean.

- [ ] **Step 6: Commit** `feat(history): partitioned MetricSample/MetricHourly + Postgres CI job`

---

### Task 2: Ingest hook

**Files:**
- Create: `apps/history/ingest.py`, `tests/test_history_ingest.py`
- Modify: `apps/monitoring/metrics_ingest.py` (`ingest_metrics`)

**Interfaces:**
- Consumes: `MetricSample` (Task 1).
- Produces: `record_samples(station, ts: datetime, values: dict[str, dict]) -> int` — `values` = `{key: {"min","max","avg"}}`; one `bulk_create(..., ignore_conflicts=True)` inside `transaction.atomic()` (savepoint); any `Exception` → logged (`logger.exception` only if `cache.add("history:ingest-error", 1, 3600)` succeeded, else `logger.debug`), returns 0; never raises. Constant `INGEST_ERROR_CACHE_KEY = "history:ingest-error"`.

- [ ] **Step 1: Failing tests** in `tests/test_history_ingest.py` (use `ingest_metrics` like `tests/test_metrics_ingest.py`, and one through the real heartbeat endpoint like `tests/test_heartbeat_metrics.py`):
  - `test_accepted_values_recorded`: defined key `pwr.12v.voltage_v` with `{"min":11.8,"max":12.3,"avg":12.1}` → one `MetricSample` with those floats, `ts` == the telemetry `ts`.
  - `test_blocked_not_recorded`, `test_out_of_range_not_recorded` (definition `min_value=0,max_value=20`, value 25), `test_undefined_key_not_recorded` (stored in telemetry but no sample), `test_hidden_is_recorded`, `test_legacy_inventory_values_recorded` (`cpu.temp_c` from `inventory={"cpu":{"temperature_c":50}}` with a definition).
  - `test_duplicate_heartbeat_same_ts_ignored`: call `record_samples` twice with identical ts → count 1, no exception.
  - `test_history_error_isolated`: monkeypatch `MetricSample.objects.bulk_create` to raise → `ingest_metrics` returns normally, telemetry saved; with `transaction.atomic()` wrapped around the call, a follow-up query in the same atomic block still works; two failures log `exception` once (caplog).
  - `test_heartbeat_200_when_history_fails` (endpoint).
  - PG-only `test_missing_partition_isolated`: drop the current-month partition of `history_metricsample`, ingest → no raise, telemetry saved, error logged once.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** In `ingest_metrics` collect `accepted: dict[str, dict]` in the loop (only branch where `definition is not None` and the value passed both checks), and after the telemetry save call `record_samples(station, now, accepted)` (lazy import `from apps.history.ingest import record_samples` to keep the monitoring→history dependency at call time). `ts` = the same `now` used for `stored[...]["ts"]`.

- [ ] **Step 4: Run** the new tests + `tests/test_metrics_ingest.py tests/test_heartbeat_metrics.py tests/test_agent_heartbeat_metrics.py` on SQLite and the history file on PG → PASS.

- [ ] **Step 5: Commit** `feat(history): record accepted metric values per heartbeat`

---

### Task 3: Compaction, retention, `history_maintain`

**Files:**
- Create: `apps/history/maintenance.py`, `apps/history/management/__init__.py`, `apps/history/management/commands/{__init__,history_maintain}.py`, `tests/test_history_maintenance.py`

**Interfaces:**
- Consumes: Task 1 models + `partitions`.
- Produces:
  - `compact(now: datetime | None = None, *, chunk_hours: int = 24) -> int` — compacts `[start, hour_floor(now))` where `start = HistoryState.compacted_until` or, if None, the hour floor of the oldest `MetricSample.ts` (nothing → return 0, leave state None). Per chunk: one aggregate query grouped by `station_id, metric_key, TruncHour("ts", tzinfo=UTC)` with `Min("min"), Max("max"), Avg("avg"), Count("*")`, upserted via `bulk_create(update_conflicts=True, unique_fields=["station","metric_key","ts"], update_fields=["min","max","avg","count"])`, then `compacted_until = chunk_end` saved (atomic per chunk). Returns rows upserted. Only closed hours (`< hour_floor(now)`).
  - `ensure_future_partitions(now=None) -> list[str]` — current + next month for both tables.
  - `drop_expired(now=None) -> list[str]` — raw: drop partitions with `hi <= now - HISTORY_RAW_RETENTION_DAYS` **and** `hi <= compacted_until` (else skip + `logger.warning` naming the partition); hourly: `hi <= now - HISTORY_HOURLY_RETENTION_DAYS`. Non-PG fallback: equivalent row delete `ts < month_start(cutoff)` with the same compacted guard (so SQLite tests cover the boundary logic).
  - `run_maintenance(now=None) -> dict` — order compact → ensure → drop; each step in its own try/except (`logger.exception`), returns `{"compacted": n, "created": [...], "dropped": [...], "errors": [step names]}`.
  - Command `history_maintain` with `--loop` (store_true) and `--interval` (int, default 600), same shape as `apps/monitoring/management/commands/check_alerts.py`; prints a one-line summary per run.

- [ ] **Step 1: Failing tests** (`tests/test_history_maintenance.py`, fixed `now`):
  - Compaction: samples at 10:05 (min 11, max 13, avg 12), 10:35 (10, 12.5, 11), 11:10 → after `compact(now=11:20)` one hourly row for 10:00 with `min 10, max 13, avg 11.5, count 2`; no row for 11:00 (open hour); `compacted_until == 11:00`. Running again → same values, returns 0 new chunks' worth (idempotent; row count unchanged). A later sample in 11:xx then `compact(now=12:01)` → 11:00 row. Hourly `ts` is UTC hour start even for Vienna-tz inputs.
  - Long outage: `compacted_until` 5 days back, `chunk_hours=24` → completes, state at current hour floor.
  - Retention (SQLite fallback + PG): with `now = 2026-11-05`, raw rows in Sept (`hi = Oct 1`, 35 d old) dropped, Oct rows kept (hi = Nov 1 not 30 d old); when `compacted_until` is before Oct 1 the Sept data is kept and a warning logged. Hourly: month whose end is >730 d old dropped, newer kept.
  - PG: `ensure_future_partitions` creates missing next-month partition; `drop_expired` actually removes partition names from `list_partitions`.
  - `run_maintenance` with `compact` monkeypatched to raise → partitions still ensured, `"compact"` in `errors`.
  - Command: `call_command("history_maintain")` runs once and writes the summary.

- [ ] **Step 2–4:** run → FAIL, implement, run on SQLite + PG → PASS.

- [ ] **Step 5: Commit** `feat(history): hourly compaction, partition upkeep and retention command`

---

### Task 4: History API

**Files:**
- Create: `apps/history/query.py`, `tests/test_history_api.py`
- Modify: `apps/api/read_views.py` (`StationViewSet`), `apps/api/read_serializers.py`

**Interfaces:**
- Consumes: Task 1 models, `MetricDefinition`.
- Produces:
  - `query.RESOLUTIONS = {"raw": 60, "5m": 300, "1h": 3600, "1d": 86400}` (seconds; raw nominal = heartbeat ~1/min), `MAX_POINTS = 1000` (auto target), `HARD_MAX_POINTS = 5000` (explicit), `MAX_KEYS = 10`, `MAX_SPAN = timedelta(days=HISTORY_HOURLY_RETENTION_DAYS)`, `DEFAULT_SPAN = timedelta(hours=24)`.
  - `query.choose_resolution(requested: str, since, until, now) -> tuple[str, str]` → `(resolution, source)` where source ∈ `"raw" | "hourly"`. auto = smallest resolution with `span/step <= MAX_POINTS`. If resolution < 1h and `since < now - HISTORY_RAW_RETENTION_DAYS` → `("1h", "hourly")`. `1h`/`1d` → hourly.
  - `query.fetch_series(station, keys: list[str], since, until, resolution, source) -> dict[str, list[list]]` — points `[epoch_s: int, min, avg, max]` sorted by ts. raw → rows as is; 5m → bucket raw rows in Python (floor epoch to 300); 1h → hourly rows as is; 1d → DB aggregate on hourly `TruncDay(tzinfo=UTC)` with `min(min)`, `max(max)`, count-weighted avg `Sum(avg*count)/Sum(count)`. 5m buckets: min-of-mins, max-of-maxs, avg-of-avgs.
  - API: `GET /api/v1/stations/{id}/metrics/history/` = `@action(detail=True, url_path="metrics/history")` named `metrics_history`. Query params: `keys` (comma list; default = keys in the station's current `telemetry.metrics` that have a non-hidden, non-blocked definition), `since`/`until` (ISO 8601; default until=now, since=until-24h; naive → UTC), `resolution` (`raw|5m|1h|1d|auto`, default auto). 400 (field-keyed `{"keys": [...]}` etc.) for: unknown resolution, unparsable dates, `since >= until`, span > `MAX_SPAN`, > `MAX_KEYS` keys, invalid key syntax (`station_agent.metrics_contract.is_valid_key`), explicit resolution yielding > `HARD_MAX_POINTS`. Response:
    ```json
    {"station": 1, "since": "...Z", "until": "...Z", "resolution": "5m", "source": "raw",
     "series": [{"key": "pwr.12v.voltage_v", "label": "...", "unit": "V", "precision": 2,
                 "group": "power", "points": [[1762300800, 11.8, 12.1, 12.3]]}]}
    ```
    Label = `display_label`; keys without definition still returned with `label=key, unit="", precision=1, group="other"`. Serializers `MetricHistoryQuerySerializer` (input) + `MetricHistorySerializer` / `MetricHistorySeriesSerializer` (output) for drf-spectacular (`@extend_schema(parameters=[MetricHistoryQuerySerializer], responses=MetricHistorySerializer)`).

- [ ] **Step 1: Failing tests** (`tests/test_history_api.py`; reuse fixtures/helpers of `tests/test_api_read_stations.py` and `tests/test_api_read_fixtures.py` for tokens/users):
  - Permission matrix identical to the telemetry action: for each user/token fixture used by the telemetry tests, assert `GET .../metrics/history/` status == `GET .../telemetry/` status (where telemetry 404s for missing row, compare 200/403/401 only on a station with telemetry); anonymous → 401/403 same as telemetry; nonexistent station → 404.
  - `choose_resolution` unit tests: 1 h span auto → `raw`; 24 h → `5m`; 7 d → `1h`; 365 d → `1d`; `5m` with since 40 d ago → `("1h","hourly")`.
  - Data: raw samples at 10:01/10:02/10:06 → `resolution=5m` gives 2 buckets with correct min/avg/max; `1h` from hourly rows; `1d` weighted avg (`avg 10 count 1`, `avg 20 count 3` → 17.5).
  - Defaults: no `keys` → hidden metric excluded; explicit hidden key → included.
  - Limits: 11 keys → 400 `keys`; span 800 d → 400; `resolution=raw` over 7 d (10080 > 5000) → 400; `since>=until` → 400; garbage date → 400.
  - Schema: `/api/v1/schema/` contains `/api/v1/stations/{id}/metrics/history/` with parameters `keys`, `since`, `until`, `resolution` (see `tests/test_api_read_schema.py` for how).

- [ ] **Step 2–4:** run → FAIL, implement, run (+ `tests/test_api_read_*.py`) → PASS.

- [ ] **Step 5: Commit** `feat(history): metrics history API with server-side downsampling`

---

### Task 5: UI charts in the Metrics tab

**REQUIRED:** invoke `Skill("frontend-design")` before writing UI code; match the existing design tokens (`static/css/tokens.css`, `.panel`, `.mx-*`, `check_color_tokens.py` must pass).

**Files:**
- Create: `static/vendor/uplot/uPlot.iife.min.js`, `static/vendor/uplot/uPlot.min.css`, `static/vendor/uplot/LICENSE` (from npm `uplot@1.6.32` `dist/`), `static/js/metrics-history-logic.js`, `static/js/metrics-history.js`, `tests/js/metrics-history-logic.test.mjs`, `tests/test_metrics_history_js.py`, `tests/test_station_detail_history.py`
- Modify: `apps/stations/templates/stations/_metrics_panel.html`, `apps/stations/templates/stations/station_detail.html` (script tags with `nonce="{{ csp_nonce }}"`, `{% static %}`), `apps/stations/views.py` (`metric_groups` rows already have `key`; add per-group `keys` list if convenient), CSS in `static/css/app.css` (or wherever `.mx-*` lives).

**Interfaces:**
- Consumes: Task 4 API response shape.
- Produces: `metrics-history-logic.js` (UMD/ESM-compatible like `audio-logic.js`) exporting `RANGES = {"1h":3600,"24h":86400,"7d":604800,"30d":2592000,"1y":31536000}`, `buildUrl(stationId, keys, rangeKey, nowMs) -> string` (`/api/v1/stations/<id>/metrics/history/?keys=a,b&since=<ISO>&until=<ISO>&resolution=auto`), `toUplotData(series) -> [xs, mins, avgs, maxs]` (null-safe; empty series → empty arrays), `formatValue(v, precision) -> string` (dot decimal, `v.toFixed(precision)`, `"—"` for null/NaN, never `toLocaleString`).

Behavior: each group block in the Metrics tab gets a collapsible "History" (`<details>`) with range buttons 1h/24h/7d/30d/1y (default 24h). On first open (and on range change) JS fetches via session auth (`credentials: "same-origin"`), renders one small uPlot chart per metric in the group: avg line + min…max band (uPlot `bands` between min/max series, band fill semi-transparent), axis values via `formatValue`, timestamps local time. Hidden metrics are never in the panel (already filtered by `metric_groups`). Error → inline message in the panel; empty series → "No history yet" empty state. No inline `<script>` without nonce; no CDN.

- [ ] **Step 1: Failing tests**
  - `tests/js/metrics-history-logic.test.mjs` + runner `tests/test_metrics_history_js.py` (same pattern as `tests/test_audio_logic_js.py`): `buildUrl(5, ["a.b","c"], "24h", Date.UTC(2026,10,5,12))` contains `since=2026-11-04T12:00:00.000Z`, `until=2026-11-05T12:00:00.000Z`, `keys=a.b%2Cc` or `keys=a.b,c` (pick one, assert it); `toUplotData([])` → four empty arrays; `toUplotData([[1,1,2,3]])` → `[[1],[1],[2],[3]]`; `formatValue(12.345, 2) === "12.35"`, `formatValue(null, 1) === "—"`.
  - `tests/test_station_detail_history.py`: station detail with metrics renders `data-history-station="<pk>"`, `data-history-keys` containing the group's keys, range buttons, the `<details>`; script tags reference `vendor/uplot/uPlot.iife.min.js` and `js/metrics-history.js` with a nonce; no `cdn.jsdelivr.net/npm/uplot`; hidden metric key absent from `data-history-keys`; station without metrics renders no history block. Static files exist on disk.

- [ ] **Step 2–4:** run → FAIL, implement, run + `tests/test_station_detail_metrics.py`, `python scripts/check_template_comments.py`, `python scripts/check_color_tokens.py` → PASS. Manually sanity check by rendering the page in `runserver` with fake data if feasible (screenshot optional).

- [ ] **Step 5: Commit** `feat(history): metric history charts in the station Metrics tab`

---

### Task 6: Docs + finish

**Files:** Modify `CLAUDE.md` (repo) — short section "Telemetrie-Historie (`apps/history`)" with the invariants (partitioned on PG only, migration +3 months, `history_maintain --loop --interval 600` compose service, compaction watermark guards raw drops, ingest savepoint/error isolation, API limits).

- [ ] Full suite `.venv/bin/python -m pytest -q` + PG history run + ruff → green.
- [ ] Commit `docs(history): architecture notes`; PR body names the servers compose service command: `python manage.py history_maintain --loop --interval 600`.
