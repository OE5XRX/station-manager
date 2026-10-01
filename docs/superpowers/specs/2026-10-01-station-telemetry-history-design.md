# Station Telemetry History — Design Spec

**Status:** Approved design, **implementation gated** (see §10 Sequencing).
**Date:** 2026-10-01
**Repo:** OE5XRX/station-manager
**Branch:** `feature/station-telemetry-history`

## 1. Problem & Goal

Today station values exist only as "current"/last-known state:
`Station.last_seen/current_os_version/current_agent_version/status`,
`StationModule.online/last_state` (a telemetry snapshot), and
`StationInventory.data` (CPU/RAM/Disk as an unindexed JSON blob). Nothing
numeric is kept over time. When a bench CM4 rebooted repeatedly, nobody could
look back at what the values were doing around the incident.

**Goal:** give every station a queryable *time axis* under its numeric
telemetry — trends (CPU temp, RAM/disk, uptime, wear), alert forensics, and
basic operational reporting — surfaced natively in station-manager, scoped by
the same SSO/topology rules as everything else.

**Non-goal:** replacing the discrete status/event history. Status transitions
(online/offline, slot a→b, OTA result, reboot reason) already live in
`StationAuditLog` and stay there (see §4 split).

### Why extend station-manager instead of exporting to Grafana

Decided: **extend natively.** Grafana would add a second stack (Prometheus +
scraping + auth + dashboards) against a deployment reality that deliberately
avoids extra services (self-hosted runner, no SSH, no Watchtower). The decisive
reason is access control: history must be **topology-scoped and SSO-bound** — a
user may only see *their* stations via `accessible_stations`. That logic already
lives in station-manager; rebuilding it in Grafana is expensive and brittle. The
data already arrives via the heartbeat, and trends render directly on the
existing Station-Detail page next to everything else.

*Deferred, cheap future bolt-on:* a read-only export endpoint (Prometheus text
or JSON) can be added later if someone wants Grafana on top — near-zero cost,
not built now (YAGNI).

## 2. Scope

**In scope (this feature):**
- Append-only numeric time-series store (`StationMetric`) with native
  monthly range-partitioning.
- An ingest hook `record_metrics()` — a single function the heartbeat path
  calls to append metric rows (contract, not an intrusion into the heartbeat
  view).
- A code-side `METRIC_CATALOG` (unit/label/aggregation per `metric_key`).
- Downsample-on-read query API (`bucketed(...)`).
- Topology-scoped trend API endpoint (depends on user-automation-api).
- "Verlauf" visualization section on the Station-Detail page.
- Retention via partition-drop (management command + scheduled job).

**Out of scope:**
- Expanding *what* the agent reports (owned by `feature/station-telemetry-alerting`
  / `feature/image-debug-observability`). We historize existing numeric fields;
  new fields plug in without schema changes (§3).
- Discrete event/status history (stays in `StationAuditLog`).
- Pre-aggregated rollup tables (deferred, §5).
- Alerting (owned by the telemetry-alerting session).

## 3. Architecture — Components & Data Flow

New Django app `history` (under `apps/`). Four units with clear boundaries:

1. **Ingest hook** — `history.ingest.record_metrics(station, payload, ts)`:
   extracts numeric fields from the heartbeat payload and appends
   `StationMetric` rows. The telemetry-alerting session calls this one function
   at the end of its heartbeat view (or we wire it via signal later). The other
   session only needs to know about *one* call. Missing fields are skipped, never
   nulled (feature-detect + graceful degrade, matching the station-telemetry
   contract).
2. **Store** — `StationMetric` + manager method
   `bucketed(station, keys, since, until, resolution)` for append + aggregated read.
3. **Retention job** — management command `prune_metrics`, run via scheduled job
   (Kontor cron / systemd timer); drops partitions older than
   `HISTORY_RETENTION_DAYS`.
4. **Query/API + UI** — trend endpoint (§6) and Station-Detail charts (§7).

**Write path:** Agent → `POST /api/v1/heartbeat/` → (telemetry session: update
`current_*` + alert) → `record_metrics(...)` → append-only `StationMetric`.
**Read path:** Station-Detail / API → `bucketed(...)` → AVG/MAX/LAST
downsample-on-read → chart.

## 4. Data Model

```python
class StationMetric(models.Model):
    station    = ForeignKey("stations.Station", on_delete=CASCADE)  # indexed
    ts         = DateTimeField()                                    # UTC, indexed
    metric_key = CharField(max_length=64)   # e.g. "cpu.temp_c", "mem.used_pct"
    value      = FloatField()               # numeric, always double

    class Meta:
        constraints = [UniqueConstraint("station", "metric_key", "ts")]
        indexes = [Index(fields=["station", "metric_key", "ts"])]
        # Partition: RANGE on ts, monthly (native Postgres declarative partitioning)
```

- **Numeric only** (`value` FloatField). Non-numeric states (slot `a/b`, OTA
  result, reboot reason) are **not** stored here.
- **One `metric_key` column**, not split into domain/name. The dot-convention
  gives the hierarchy for free; `LIKE 'cpu.%'` covers the rare domain query.
  Splitting would only widen every row and the composite index for no real
  query benefit at this scale.
- **unit/label/agg live in code, not the DB** — they are constant per
  `metric_key`, so storing them per row would duplicate the same string across
  millions of rows. They belong in `METRIC_CATALOG` (§5).
- **Partitioning:** monthly RANGE partitions on `ts`. Retention = drop the old
  partition (no `DELETE` over millions of rows). Implemented with **hand-written
  SQL in a migration — no new dependency** (preferred over `django-postgres-extra`;
  revisit only if partition management becomes painful).
- **No TimescaleDB** — keeps the stack dependency-free (Postgres 17, soon 18).

### The numeric-vs-event split (contract)

- **Time-series → `StationMetric`:** anything that is a meaningful *number over
  time* (temperature, fill levels, uptime, wear %).
- **Event/status → `StationAuditLog`:** discrete state transitions
  (online/offline, slot a→b, OTA success/rolled_back, reboot reason
  crash/watchdog). These have no meaningful AVG curve — they are points on a
  timeline. **Not duplicated.**
- **Grey zone — undervoltage/throttle:** the raw `get_throttled` bitmask runs as
  a *number* in `StationMetric` (for "how often was there undervoltage last
  week"); the *alert event* stays in the audit/monitoring path of the telemetry
  session. One source, two views, no duplicated logic.

## 5. Metric Catalog & Retention

### Catalog (code-side single source of truth)

Naming convention: `domain.name_unit`, lowercase, dot-separated, unit in the
name so the chart knows what it plots without a lookup table.

```python
METRIC_CATALOG = {
    "cpu.temp_c":         {"unit": "°C",  "label": "CPU-Temperatur",  "agg": "avg"},
    "mem.used_pct":       {"unit": "%",   "label": "RAM-Auslastung",  "agg": "avg"},
    "disk.used_pct":      {"unit": "%",   "label": "Disk-Auslastung", "agg": "avg"},
    "sys.uptime_s":       {"unit": "s",   "label": "Uptime",          "agg": "last"},
    "sys.load1":          {"unit": "",    "label": "Load (1 min)",    "agg": "avg"},
    "emmc.life_used_pct": {"unit": "%",   "label": "eMMC-Verschleiß", "agg": "max"},
    "pwr.throttled_bits": {"unit": "",    "label": "Throttle-Bits",   "agg": "max"},
}
```

This is the initial known set; the full field authority is the telemetry
session. **Adding a numeric field = one line here + one entry in the
`HEARTBEAT_FIELD → metric_key` map in `record_metrics()`** — no schema migration.
Unknown `metric_key`s that arrive without a catalog entry are still stored (store
stays generic); they just render without a nice label until catalogued.

### Retention

- **Default raw retention 365 days**, then partition-drop. Configurable via
  `HISTORY_RETENTION_DAYS` (setting, not hardcoded).
- Ceiling estimate: <20 stations × ~7 numeric metrics × 1440/day ≈ 150M rows/year
  — fine for Postgres with monthly partitions and a narrow row
  (int FK, ts, varchar(64), float8); realistically far less.
- **Downsample-on-read, not rollup tables:** the query buckets at read time via
  `date_trunc`/`width_bucket` + AVG/MAX/LAST (per catalog `agg`) into the
  client-requested resolution. No pre-aggregate, no second table, no
  materialized-view refresh job.
- **Rollup tables deferred (YAGNI):** only if a 1-year trend query becomes
  measurably slow (measured, not assumed) do we add a continuous monthly
  aggregate. Tracked as an explicit "Deferred" item so it is neither forgotten
  nor built prematurely.

## 6. Query / API

Depends on `feature/user-automation-api` (hence gated — §10).

```
GET /api/v1/stations/{id}/metrics
    ?keys=cpu.temp_c,mem.used_pct
    &since=<iso8601>&until=<iso8601>
    &resolution=5m
```

- **Topology-scoped** via `accessible_stations` (helper from the API session):
  403/404 if the station is not in the token's scope. No new access logic.
- Response: one series per `metric_key` as `[[ts, value], …]`, plus catalog
  metadata (unit/label) for axis labelling.
- `drf-spectacular`-annotated (`/api/v1/schema` + `/docs`), as established by the
  API session.
- Throttle: existing `api-token` scope.

## 7. Visualization (Station-Detail page)

- A "Verlauf" tab/section on the existing Station-Detail page with a time-range
  selector (1h / 24h / 7d / 30d) and a multi-series chart.
- **Chart lib:** a lean, dependency-light one — **uPlot** (tiny, very fast with
  many points) is the default recommendation; Chart.js is the heavier, more
  familiar alternative. Final pick during implementation; no heavyweight
  framework. HTMX lazy-loads the chart section; the chart fetches JSON from the
  API endpoint.
- **DE-locale decimal trap** (per CLAUDE.md): axes/values forced dot-decimal.
- The frontend work **must** use the `frontend-design` skill (CLAUDE.md rule for
  pixel/UI work).

## 8. Error Handling & Edge Cases

- `record_metrics()` skips absent fields (qemu payload without `vcgencmd`,
  pre-merge, SD-instead-of-eMMC) — never hard-fails, never writes nulls.
- De-duplication on `(station, metric_key, ts)` via the unique constraint; a
  replayed heartbeat is idempotent.
- Empty buckets in a range render as gaps, not zeros.
- All timestamps UTC end-to-end; locale formatting only at the view layer.
- Ingest must not break the heartbeat response if the history write fails — the
  hook isolates its own errors (log + continue), so telemetry/alerting stay
  authoritative.

## 9. Testing

- **Store/Ingest:** `record_metrics()` extracts correctly, skips missing fields
  (graceful degrade), dedupes on `(station, key, ts)`.
- **Bucketing:** downsample query returns correct AVG/MAX/LAST per bucket; edge
  cases: empty buckets, timezone (UTC).
- **API:** topology scoping (foreign station → 403/404), token auth, schema
  validation against drf-spectacular, throttle.
- **Retention:** partition-drop removes only the target month, leaves current
  data untouched.
- **Integration (probe, E2E):** simulated heartbeat payload → `record_metrics`
  → query → expected series.
- Conventions: tests in top-level `tests/test_*.py`, `python -m pytest -q`
  (settings `config.settings.test`).

## 10. Sequencing & Dependencies

Implementation is **gated** until two in-flight efforts land on `main`:

1. **`feature/user-automation-api`** (PR #149, coordinator 977d47c2) — provides
   Bearer-auth, the `api-token` throttle scope, drf-spectacular, and the
   `accessible_stations`/`accessible_regions` topology helper that §6 reuses.
   History trend endpoints are a natural Phase-2/3 consumer of this API.
2. **`feature/station-telemetry-alerting`** (coordinator a05e1114) — defines the
   expanded heartbeat fields and the current-value ingest path that §3's hook
   taps. It is expected to land a *current-value* model + Station-Detail UI +
   alerting; this feature adds the *time axis* beneath the same values.

**This feature deliberately does not modify either session's code.** It defines
the `record_metrics()` contract and the metric catalog; wiring the call into the
heartbeat view happens after telemetry-alerting merges (one call site, or a
`post_save`/signal). Until then this spec is the ready artifact.

**Coordination note:** if the telemetry-alerting session's current-value model
turns out to be cheaply extensible to append-only, revisit whether to fold the
time-series in there instead of a separate layer — but the default remains the
decoupled `history` app so neither in-flight session's scope grows.

## 11. Open Decisions (resolve at implementation time)

- Partition management: hand-written SQL migration vs. a helper library —
  default hand-written, no new dep.
- Chart library: uPlot (default) vs. Chart.js.
- Retention default: 365 days (confirm against real row growth once telemetry
  fields are final).
- Whether the trend endpoint lives in the `history` app or the API app's
  versioned router (depends on how user-automation-api structures its routers).
