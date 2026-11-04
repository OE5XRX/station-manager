# Generic Metrics + Threshold Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agent-described generic `metrics` in the heartbeat, stored as current values + `MetricDefinition` rows, evaluated by generic duration/hysteresis threshold rules that replace the CPU/Disk/RAM checks; UI + API to manage rules and definitions; agent collector incl. PowerBoard (INA226 via hwmon).

**Architecture:** A pure-stdlib contract module in `station_agent/` (key regex, mount→instance, value normalisation, descriptor hash) is imported by both agent and server (server already imports `station_agent.audio.frame`). Server: `MetricDefinition` + `ThresholdState` in `apps/monitoring`, `StationTelemetry.metrics` + `metric_descriptors_hash` in `apps/stations`; `ingest_metrics` runs fault-isolated in the heartbeat view after `ingest_telemetry`; `_check_thresholds` in the engine replaces the three legacy checks. Agent: a `MetricsCollector` with a ~1 Hz sampler thread, shared by the main and keepalive heartbeat paths.

**Tech Stack:** Django 6.0, DRF 3.17, drf-spectacular, HTMX/Bootstrap templates; Python 3.14 stdlib-only agent.

**Spec:** `docs/superpowers/specs/2026-11-03-generic-metrics-design.md` (binding; do not renegotiate design decisions).

## Global Constraints

- Key regex `^[a-z0-9_]+(\.[a-z0-9_]+){1,3}$`, max 64 chars; max 200 keys per heartbeat (excess dropped + logged).
- Descriptor hash = SHA-256 hex over `json.dumps(sorted(descriptors, key=lambda d: d["key"]), sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
- Groups: `system`, `disk`, `network`, `power`, `module`, `other` (unknown → `other`); label ≤ 64 chars.
- Global cap 2000 `MetricDefinition` rows; over cap: no new rows, values dropped, warning logged once per key per hour.
- Value = number or `{"min","max","avg"}`; scalar → min=max=avg. Missing source → key omitted, never `null`/0.
- Telemetry/metrics ingest must never break the heartbeat (try/except + `logger.exception`, like `ingest_telemetry`).
- Agent: no new Python dependency; every source feature-detected + graceful degrade; never touch control devices (`/dev/oe5xrx/slot*/control`).
- Templates: never multi-line `{# #}`, use `{% comment %}`. All number inputs `lang="en"` + dot-decimal (`|unlocalize`).
- UI work MUST invoke `Skill("frontend-design")` first.
- Tests top-level `tests/test_*.py`; run `python -m pytest -q`; lint `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .`.
- Permissions for rules + metric management: `area="alert_rules"`, writes `verb="manage"` (UI `AreaPermissionMixin`, API `AreaWriteViewSet`).
- Commits: only `git add` your own task's files (two implementers may share the worktree); retry on `index.lock`.

## Review Focus

1. Agent sends a value for a key whose descriptor it never sends → server must not re-request descriptors forever when the agent just sent its list (request only on hash mismatch, or unknown keys while under cap and no list in this heartbeat). Test in Task 3.
2. `metrics` value of wrong shape (string, nested dict without avg, bool, NaN/inf, dict missing keys) → that key dropped, heartbeat 200, other keys stored. Test in Task 3.
3. Station offline / metric missing for a while then back → threshold state frozen, no spurious resolve or fire. Test in Task 4.
4. Rule edited in UI/API/admin while an alert is open → alert auto-resolved, state reset (signals cover admin too). Test in Task 4.
5. hwmon device present but `in1_input` unreadable / sampler thread exception → key omitted, sampler keeps running, heartbeat still sent. Test in Task 7.

---

## File Map

| File | Responsibility |
|---|---|
| `station_agent/metrics_contract.py` (new) | Shared pure contract: `KEY_RE`, `MAX_KEY_LEN=64`, `MAX_KEYS=200`, `GROUPS`, `mount_to_instance`, `normalize_value`, `descriptors_hash`, `clean_descriptor`, `key_matches_pattern`. |
| `apps/monitoring/models.py` | `AlertRule` extension, `MetricDefinition`, `ThresholdState`. |
| `apps/monitoring/migrations/0005…`, `0006…` | Schema; data migration (legacy defs, rule conversion, resolve open alerts, 12 V rule). |
| `apps/stations/migrations/0030…` | `StationTelemetry.metrics`, `metric_descriptors_hash`. |
| `apps/monitoring/metrics_ingest.py` (new) | `ingest_metrics`, descriptor upsert, legacy inventory fallback. |
| `apps/monitoring/rules.py` (new) | `validate_metric_key`, `reset_rule_evaluation`, formatting helpers. |
| `apps/monitoring/signals.py` (new) | pre_save/pre_delete on AlertRule → reset evaluation. |
| `apps/monitoring/engine.py` | `_check_thresholds` replacing cpu/disk/ram checks. |
| `apps/api/serializers.py`, `apps/api/views.py` | Heartbeat fields + response flag. |
| `apps/api/read_serializers.py`, `write_serializers.py`, `read_views.py`, `write_views.py`, `router.py` | AlertRule fields, metric definitions endpoint, station `metrics`. |
| `apps/monitoring/views.py`, `urls.py`, templates | Threshold rule CRUD, metric management page. |
| `apps/stations/views.py`, `station_detail.html` (+ partial) | "Metrics" tab. |
| `station_agent/metrics.py` (new) | `MetricsCollector`, sampler thread, collectors, descriptors. |
| `station_agent/heartbeat.py`, `agent.py` | Wire collector, RTT, descriptor resend. |

---

### Task 1: Shared metrics contract module

**Files:**
- Create: `station_agent/metrics_contract.py`
- Test: `tests/test_metrics_contract.py`

**Interfaces — Produces:**
- `KEY_RE: re.Pattern`, `MAX_KEY_LEN = 64`, `MAX_KEYS = 200`, `GROUPS = ("system","disk","network","power","module","other")`
- `is_valid_key(key: object) -> bool`
- `mount_to_instance(mount: str) -> str`
- `normalize_value(value: object) -> dict[str, float] | None` — returns `{"min","max","avg"}` floats or None (bool, non-finite, wrong shape → None).
- `clean_descriptor(d: object) -> dict | None` — returns `{"key","label","unit","group","precision","min","max"}` (min/max may be None, precision int 0..6 default 1, group coerced to `other`, label truncated to 64, unit ≤ 16) or None if key invalid/not a dict.
- `descriptors_hash(descriptors: list[dict]) -> str` (spec formula verbatim).
- `key_matches_pattern(key: str, pattern: str) -> bool` — pattern has ≤1 `*` segment matching exactly one segment; no `*` → equality.
- `is_valid_pattern(pattern: str) -> bool` — valid key, or same shape with exactly one segment == `*`.

- [ ] **Step 1: Failing tests** in `tests/test_metrics_contract.py`:
  - `mount_to_instance("/") == "root"`, `"/boot" → "boot"`, `"/data/log" → "data_log"`, `"/Data-X" → "data_x"`.
  - `is_valid_key`: `cpu.temp_c` ✓, `pwr.12v.voltage_v` ✓, `a.b.c.d.e` ✗ (5 segments), `cpu` ✗, `CPU.x` ✗, 65-char key ✗, `None` ✗.
  - `normalize_value(5) == {"min":5.0,"max":5.0,"avg":5.0}`; `{"min":1,"max":3,"avg":2}` ok; `True`, `"5"`, `float("nan")`, `{"min":1}`, `{"min":1,"max":"x","avg":2}` → None.
  - `descriptors_hash` equals `hashlib.sha256(json.dumps(sorted(ds,key=…),sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()` and is order-independent.
  - `clean_descriptor({"key":"x.y","label":"L"*80,"group":"weird"})` → label len 64, group `other`; invalid key → None.
  - `key_matches_pattern("disk.root.used_pct","disk.*.used_pct")` ✓, `("disk.a.b.used_pct","disk.*.used_pct")` ✗; `is_valid_pattern("disk.*.*")` ✗, `("*.x")` ✓.
- [ ] **Step 2:** `python -m pytest -q tests/test_metrics_contract.py` → FAIL (module missing).
- [ ] **Step 3:** Implement (stdlib only: `re`, `json`, `hashlib`, `math`).
- [ ] **Step 4:** Tests pass; ruff clean.
- [ ] **Step 5:** Commit `feat(metrics): shared metrics contract module`.

---

### Task 2: Server models + migrations

**Files:**
- Modify: `apps/monitoring/models.py`, `apps/stations/models.py`
- Create: `apps/monitoring/migrations/0005_generic_metrics_schema.py`, `0006_generic_metrics_data.py`, `apps/stations/migrations/0030_stationtelemetry_metrics.py`
- Modify: `apps/monitoring/migrations/0002_seed_default_rules.py` (docstring only — it must stay runnable on fresh DBs; conversion happens in 0006), `apps/monitoring/management/commands/create_default_alert_rules.py`, `apps/monitoring/admin.py`
- Test: `tests/test_metrics_models.py`, `tests/test_metrics_migration.py` (use `django_test_migrations` if present in requirements, else `MigrationExecutor` like existing `tests/test_monitoring_seed.py` — check that file's pattern first)

**Interfaces — Produces:**
- `AlertRule.AlertType`: remove `CPU_TEMPERATURE`, `DISK_WARNING`, `DISK_CRITICAL`, `RAM_CRITICAL`; add `THRESHOLD = "threshold", _("Threshold")`. `alert_type` loses `unique=True`; add `UniqueConstraint(fields=["alert_type"], condition=~Q(alert_type="threshold"), name="monitoring_alertrule_unique_event_type")`.
- New `AlertRule` fields: `name` CharField(100, blank), `metric_key` CharField(64, blank), `operator` CharField(3, choices `gt/gte/lt/lte`, blank), `aggregate` CharField(3, choices `avg/min/max`, default `avg`), `duration_seconds` PositiveIntegerField(default 0). Ordering `["alert_type", "metric_key", "threshold"]`. `__str__` for threshold: `f"{name or metric_key} {operator} {threshold}"`. Help text for `threshold` generalised.
- `AlertRule.Operator` / `AlertRule.Aggregate` TextChoices.
- `MetricDefinition` (monitoring): `key` CharField(64, unique), `label` CharField(64, blank), `unit` CharField(16, blank), `group` CharField(16, choices GROUPS, default `other`), `precision` PositiveSmallIntegerField(default 1), `min_value`/`max_value` FloatField(null), `source_agent_version` CharField(32, blank), `first_seen` (auto_now_add), `last_seen` DateTimeField(default now), `label_override` CharField(64, blank), `min_override`/`max_override` FloatField(null), `is_hidden`/`is_blocked` BooleanField(False), `last_rejected_at` DateTimeField(null), `last_rejected_value` JSONField(null). Properties: `display_label` (override or label or key), `range_min`/`range_max` (override if not None else agent value). `MAX_DEFINITIONS = 2000` module constant.
- `ThresholdState` (monitoring): `rule` FK AlertRule CASCADE, `station` FK Station CASCADE, `metric_key` CharField(64), `breach_since` DateTimeField(null), `ok_since` DateTimeField(null), `alert` FK Alert SET_NULL null; `unique_together (rule, station, metric_key)`.
- `StationTelemetry.metrics` JSONField(default=dict, blank) — `{key: {"min","max","avg","ts"}}`; `StationTelemetry.metric_descriptors_hash` CharField(64, blank).
- Data migration 0006 (forward):
  1. Create `MetricDefinition` rows (get_or_create by key, defaults only): `cpu.temp_c` ("CPU temperature","°C",system,1,-40,125), `mem.used_pct` ("Memory used","%",system,1,0,100), `disk.root.used_pct` ("Disk used /","%",disk,1,0,100), `disk.boot.used_pct` ("Disk used /boot","%",disk,1,0,100), `sys.uptime_s` ("Uptime","s",system,0,0,None).
  2. Convert rows: `cpu_temperature` → `threshold`, `cpu.temp_c`, name "CPU temperature"; `disk_warning` → `disk.*.used_pct`, "Disk usage (warning)"; `disk_critical` → `disk.*.used_pct`, "Disk usage (critical)"; `ram_critical` → `mem.used_pct`, "Memory usage". All `operator="gte"`, `aggregate="avg"`, `duration_seconds=0`; keep existing `threshold`, `severity`, `is_active`, `description`. Same pk → `Alert.alert_rule` FKs unchanged.
  3. Resolve open alerts of those converted rules (`is_resolved=True, resolved_at=now`).
  4. Create if absent (no row with `alert_type="threshold", metric_key="pwr.12v.voltage_v", operator="lt"`): `pwr.12v.voltage_v lt 11.0 avg 60s warning`, name "12 V rail undervoltage".
  Reverse: noop (documented like 0002).
- Management command: event rules via get_or_create on `alert_type`; threshold defaults via get_or_create on `(alert_type="threshold", metric_key, operator, threshold-default name)` with the 5 threshold defaults above.

- [ ] **Step 1: Failing tests:** event-type uniqueness still enforced (IntegrityError on second `ota_failed`), two `threshold` rules allowed; `MetricDefinition.display_label`/range resolution (override wins, incl. override `0.0`); ThresholdState unique + CASCADE on rule delete; migration test: start at `monitoring 0004` with a `cpu_temperature` rule threshold 75/inactive/critical + an open Alert → after migrate rule is `threshold`/`cpu.temp_c`/75/inactive/critical, alert FK same pk and resolved, legacy definitions exist, 12 V rule exists exactly once (re-run idempotent).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement models + `makemigrations` for schema, hand-write data migration. Fix any existing tests that reference removed AlertTypes (`tests/conftest.py`, `test_monitoring*.py`, `test_api_write_alertrules.py`) — legacy cpu/disk/ram engine tests move to Task 4 (delete them here only if they reference removed code; keep event-rule tests).
- [ ] **Step 4:** `python manage.py makemigrations --check --dry-run` clean; `python -m pytest -q` green.
- [ ] **Step 5:** Commit `feat(monitoring): MetricDefinition, threshold rules, metrics storage`.

---

### Task 3: Metrics ingest + heartbeat contract (server)

**Files:**
- Create: `apps/monitoring/metrics_ingest.py`
- Modify: `apps/api/serializers.py` (HeartbeatSerializer), `apps/api/views.py`
- Test: `tests/test_metrics_ingest.py`, `tests/test_heartbeat_metrics.py`

**Interfaces:**
- Consumes: Task 1 contract, Task 2 models.
- Produces: `ingest_metrics(station, *, metrics, descriptors, descriptors_hash, agent_version, inventory) -> bool` returning `descriptors_required`. Heartbeat serializer: `metrics = _AnyJSONField(required=False)`, `metric_descriptors = _AnyJSONField(required=False)`, `metric_descriptors_hash = CharField(max_length=64, required=False, allow_blank=True)`. Response body gains `"metric_descriptors_required": true` only when True (absent otherwise).

Behaviour (in this order):
1. Descriptors (if a list): `clean_descriptor` each, drop invalid, dedupe by key (last wins), cap at `MAX_KEYS`. Compute `descriptors_hash(cleaned)`; store on `StationTelemetry.metric_descriptors_hash`. Upsert: existing key → overwrite agent fields + `source_agent_version` + `last_seen` (last writer wins; never touch override/hidden/blocked fields); new key → create only if `MetricDefinition.objects.count() < MAX_DEFINITIONS`, else rate-limited warning (Django cache key `metrics:cap:<key>`, 3600 s).
2. Values: if `metrics` is a dict, iterate first `MAX_KEYS` items (log dropped count). Skip invalid key / `normalize_value` None. Legacy fallback: derive from `inventory` only for keys not present in `metrics`: `cpu.temperature_c`→`cpu.temp_c`, `ram.usage_percent`→`mem.used_pct`, each `disk[i].usage_percent` → `disk.<mount_to_instance(mount)>.used_pct`, heartbeat `uptime` is not in inventory → use `telemetry.boot.uptime_seconds` if present else skip (`sys.uptime_s`). Only numeric (non-bool) values.
3. Per key with definition: `is_blocked` → drop. Range check: any of min/max/avg outside `[range_min, range_max]` (each bound optional) → drop, set `last_rejected_at=now`, `last_rejected_value=<normalized dict>` (one bulk update per heartbeat ok). Keys with definition: bulk-update `last_seen` at most once per heartbeat. Keys without definition: stored anyway.
4. `StationTelemetry.metrics` replaced with `{key: {min,max,avg,ts: now.isoformat()}}` via `save(update_fields=["metrics", "metric_descriptors_hash"])` (get_or_create telemetry row).
5. `descriptors_required` = (`descriptors_hash` given and no list in this heartbeat and `descriptors_hash != stored hash`) or (no list in this heartbeat and any stored key lacks a definition and def count < MAX_DEFINITIONS).
- View: call after `ingest_telemetry`, in its own `try/except Exception: logger.exception(...)`; on exception `descriptors_required=False`. Inventory passed is `data.get("inventory")`, telemetry dict for uptime via a `telemetry=` kwarg (add it to the signature: `telemetry: dict | None`).

- [ ] **Step 1: Failing tests** (`tests/test_metrics_ingest.py`, direct function): scalar + min/max/avg stored; invalid key/value dropped, others kept (Review Focus 2: `"x"`, `True`, `nan`, `{"min":1}`); >200 keys → 200 stored; blocked dropped; out-of-range (agent range) dropped + `last_rejected_*` set; override range wins over agent range (value inside override, outside agent → kept); negative current kept when range allows; key w/o definition stored + required; new descriptors create rows; second station sending different label → label overwritten (last writer wins), override untouched; cap: with 2000 defs a new key isn't created; hash stored equals contract hash; mismatched hash → required; same hash → not required; list sent + unknown value key → not required (Review Focus 1); legacy fallback: old payload (no metrics) yields `cpu.temp_c`, `mem.used_pct`, `disk.root.used_pct`, `disk.boot.used_pct`; fallback not overriding explicit metrics key; metrics replaced not merged.
- [ ] **Step 2:** `tests/test_heartbeat_metrics.py` (API, signed device request — copy fixture pattern from `tests/test_heartbeat_telemetry_e2e.py`): response contains `metric_descriptors_required: true` for unknown hash; heartbeat still 200 when `ingest_metrics` raises (monkeypatch); garbage `metrics: "x"` → 200.
- [ ] **Step 3:** Run → FAIL. **Step 4:** Implement. **Step 5:** Tests pass, full suite green, ruff. **Step 6:** Commit `feat(metrics): heartbeat metrics + descriptor ingest`.

---

### Task 4: Generic threshold engine + rule-change reset

**Files:**
- Modify: `apps/monitoring/engine.py` (delete `_check_cpu_temperature`, `_check_disk_usage`, `_check_ram_usage`; add `_check_thresholds`), `apps/monitoring/apps.py` (connect signals in `ready()`)
- Create: `apps/monitoring/rules.py`, `apps/monitoring/signals.py`
- Test: `tests/test_threshold_engine.py`; update `tests/test_monitoring.py`, `tests/test_monitoring_check_alerts_wiring.py`

**Interfaces:**
- Produces (`rules.py`): `validate_metric_key(value: str) -> None` (raises `django.core.exceptions.ValidationError`; must be valid key or valid pattern, and match ≥1 `MetricDefinition` key); `reset_rule_evaluation(rule) -> int` (resolve open alerts of rule, delete its ThresholdState rows; returns resolved count); `compare(op: str, value: float, threshold: float) -> bool`; `format_duration(seconds: int) -> str` (e.g. `"2 min"`, `"45 s"`, `"1 h 5 min"`).
- Signals: `pre_save` on AlertRule (existing pk, `alert_type == threshold` before or after) → `reset_rule_evaluation`; `pre_delete` → resolve open alerts (state cascades).

`_check_thresholds()` algorithm (now = `timezone.now()`):
- Active threshold rules; definitions `{key: def}` excluding `is_blocked`; telemetry rows `select_related("station")`; existing ThresholdState rows loaded once into a dict keyed `(rule_id, station_id, metric_key)`.
- For each station: if `station.last_seen` is None or `< now - OFFLINE_THRESHOLD` → skip (frozen). For each rule, concrete keys = keys of `tel.metrics` matching `rule.metric_key` (via `key_matches_pattern`) AND present in definitions. Keys previously in state but absent now → untouched (frozen).
- value = `tel.metrics[key][rule.aggregate]` (non-number → skip). breached = `compare(...)`.
- breached: `ok_since=None`; `breach_since = breach_since or now`; if `now - breach_since >= duration` and (`state.alert` is None or resolved) → create Alert (title `f"{def.display_label}: {station.name}"`, message per spec: `"{label} on {station}: {value}{unit} {above|below} {threshold}{unit} for {duration}"` — above for gt/gte; value rounded to `def.precision`), set `state.alert`.
- not breached: `breach_since=None`; if open `state.alert`: `ok_since = ok_since or now`; if `now - ok_since >= duration` → resolve alert, `ok_since=None`, `state.alert=None`. Without open alert: `ok_since=None`.
- Persist state rows (create/update only when changed).
- `check_alerts()` calls `_check_thresholds()` where the three old checks were.

- [ ] **Step 1: Failing tests** (`freezegun` if in requirements, else pass `now` via monkeypatching `timezone.now`): duration 0 fires immediately; duration 60 not fired at t+30, fired at t+60; one alert only while open; resolve requires ok ≥ duration (hysteresis: dip back to ok for 30 s then breach again → still open, no second alert); offline station (last_seen 10 min ago) → nothing changes (Review Focus 3), back online + still breached → no duplicate; key missing → frozen; `disk.*.used_pct` with root+boot breached → two alerts; warning+critical rules both open; state survives "restart" (`_check_thresholds` called fresh, state from DB); blocked definition → ignored; key without definition → ignored; rule update via `.save()` resolves open alert + deletes state, rule delete resolves alert (Review Focus 4); `validate_metric_key` rejects unknown key, accepts pattern matching a def, rejects `disk.*.*`; existing event-rule engine tests still pass.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Full suite green, ruff. **Step 5:** Commit `feat(monitoring): generic threshold engine with duration + hysteresis`.

---

### Task 5: API — AlertRule fields, metric definitions, station metrics

**Files:**
- Modify: `apps/api/read_serializers.py`, `apps/api/write_serializers.py`, `apps/api/read_views.py`, `apps/api/write_views.py`, `apps/api/router.py`
- Test: `tests/test_api_metrics.py`; extend `tests/test_api_write_alertrules.py`

**Interfaces:**
- `AlertRuleSerializer`/`AlertRuleWriteSerializer` fields += `name`, `metric_key`, `operator`, `aggregate`, `duration_seconds`. Write validation: `alert_type == "threshold"` requires `metric_key` (via `validate_metric_key`) and `operator`; event types must not set `metric_key`/`operator` (400 field errors). Duplicate event type → 400 (UniqueConstraint → `UniqueConstraintValidator`/explicit check). Audit messages use `str(rule)`.
- `MetricDefinitionSerializer` (read: all fields + `display_label`, `range_min`, `range_max`; writable only `label_override`, `min_override`, `max_override`, `is_hidden`, `is_blocked`; validate `min_override <= max_override` when both set).
- `MetricDefinitionViewSet`: area `alert_rules`, list/retrieve/partial_update/update only (no create/delete), registered at `r"metrics/definitions"` basename `metric-definition`; writes audited via `audit_config_write`. Filter `group`, `is_hidden`, `is_blocked`.
- `StationTelemetrySerializer` += `metrics` as a `SerializerMethodField` returning list of `{key, label, unit, group, precision, min, max, avg, ts}` only for keys with a non-hidden, non-blocked definition (one query for defs). Use `extend_schema_field` so spectacular shows the shape.

- [ ] **Step 1: Failing tests:** viewer (alert_rules.view) can GET definitions, cannot PATCH (403); manager PATCH override → 200 + audit row; POST/DELETE on definitions → 405; create threshold rule with unknown metric_key → 400 `metric_key`; with `disk.*.used_pct` → 201; event rule with metric_key → 400; second `ota_failed` → 400; telemetry endpoint shows metrics with label and hides hidden defs; `/api/v1/schema/` contains `metric_key` and `metrics/definitions`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Suite green, ruff. **Step 5:** Commit `feat(api): threshold rule fields + metric definitions endpoint`.

---

### Task 6: UI — threshold rules, metric management, station Metrics tab

**FIRST:** invoke `Skill("frontend-design")`. Match existing design language (`alert_settings.html`, `station_detail.html` tab panels, `t-table`, `t-mono`, CSS vars).

**Files:**
- Modify: `apps/monitoring/views.py`, `apps/monitoring/urls.py`, `apps/monitoring/templates/monitoring/alert_settings.html`
- Create: `apps/monitoring/forms.py` (`ThresholdRuleForm` ModelForm: name, metric_key, operator, aggregate, threshold, duration_seconds, severity, is_active, description; `clean_metric_key` → `validate_metric_key`; forces `alert_type="threshold"`), `apps/monitoring/templates/monitoring/metric_definitions.html`
- Modify: `apps/stations/views.py` (station detail context `metric_groups`), `apps/stations/templates/stations/station_detail.html` (new tab `metrics` + panel; inventory panel gets a link to it), create `apps/stations/templates/stations/_metrics_panel.html`
- Test: `tests/test_monitoring_threshold_ui.py`, `tests/test_metric_definitions_ui.py`, `tests/test_station_detail_metrics.py`

**Interfaces:**
- URLs (`monitoring:`): `threshold_rule_create` `settings/thresholds/new/` (POST), `threshold_rule_update` `settings/thresholds/<pk>/` (POST), `threshold_rule_delete` `settings/thresholds/<pk>/delete/` (POST), `metric_definitions` `metrics/` (GET), `metric_definition_update` `metrics/<pk>/` (POST). All `area="alert_rules"`; POSTs `verb="manage"`; non-threshold pk on threshold views → 404. Form errors re-render settings page with 400 or return JSON errors (follow existing AlertRuleUpdateView style: choose regular form POST + redirect with `messages` — simpler, no JS).
- `AlertSettingsView` context: `event_rules` (alert_type != threshold), `threshold_rules`, `metric_definitions` (for a `<datalist>` of keys + pattern suggestions: for each def key with ≥3 segments, the `*`-variant of the middle segment, deduped).
- `AlertRuleUpdateView` must 404 for threshold rules (they use the new views).
- Station detail `metric_groups`: ordered list `[(group_label, [rows])]` for groups system/network/power/disk/module/other, rows `{key,label,unit,precision,min,max,avg,is_aggregate (min!=max),breached}`; `breached` = any open Alert of this station whose ThresholdState.metric_key == key. Skip hidden/blocked/undefined keys. Empty → friendly empty state.
- All number inputs: `type="number" step="any" lang="en"` and values via `|unlocalize`.
- Metric definitions page: table key/agent label/unit/group/last seen/override fields/hidden/blocked + "last rejected" badge (`last_rejected_at` + value) — per-row POST form.

- [ ] **Step 1: Failing tests:** manager creates rule (302, row exists), invalid metric_key → no row + error shown; viewer POST → 403; update resets open alert (via signal); delete works; settings page renders `lang="en"` on threshold inputs and no `{#` leaks; metric page lists defs with rejected badge, PATCH-equivalent POST sets override; viewer cannot POST; station detail renders Metrics tab with power group values min/avg/max and breached highlight; template guard test (existing CI guard) passes.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Suite green, ruff, `python manage.py makemessages` not required. **Step 5:** Commit `feat(ui): threshold rules, metric management, station metrics tab`.

---

### Task 7: Agent metrics collector + sampler

**Files:**
- Create: `station_agent/metrics.py`
- Test: `tests/test_agent_metrics.py`

**Interfaces:**
- Consumes: Task 1 contract.
- Produces: `class MetricsCollector(root: str = "/", interval: float = 1.0)` with `start()`, `stop()`, `snapshot() -> tuple[dict[str, float | dict], list[dict]]` (values, descriptors — resets the 1 Hz window atomically under a lock; thread-safe for two concurrent heartbeat callers), `record_server_rtt(ms: float)`. All file paths built from `root` (tests use `tmp_path` fake sysfs/procfs).

Sources (each in its own try/except; failure → key omitted, log debug once):
- 1 Hz sampler thread (daemon, `threading.Event` stop, exceptions caught per tick): `cpu.usage_pct` from `/proc/stat` aggregate-line delta between ticks; PowerBoard: scan `sys/class/hwmon/hwmon*/device` → `os.path.realpath` basename `1-0040` (5 V) / `1-0041` (12 V); require `name` file containing `ina226` or `ina2xx` prefix; read `in1_input` mV → `pwr.<rail>.voltage_v`, `curr1_input` mA → `current_a`, `power1_input` µW → `power_w`. Re-scan device mapping every 60 s (hwmonN may appear late). Window min/max/avg per key.
- Per snapshot: `cpu.temp_c` (thermal_zone0 m°C), `cpu.freq_mhz` (`cpufreq/scaling_cur_freq` kHz), `sys.load1/5/15` (`/proc/loadavg`), `mem.used_pct` (MemTotal−MemAvailable), `swap.used_pct` (only if SwapTotal > 0), `sys.uptime_s`, `disk.<mount_to_instance(m)>.used_pct` for `/` and `/boot` (statvfs — real paths, not root-prefixed; inject via `disk_mounts` ctor arg default `("/", "/boot")`), `net.<if>.rx_kbps/tx_kbps/errors/drops` from `/proc/net/dev` deltas vs previous snapshot (first snapshot: no net rates; errors/drops = rx+tx delta; exclude `lo`, `veth*`, `docker*`, `br-*`; counter decrease → skip that interval), `net.server_rtt_ms` (last recorded RTT, omitted if none).
- Descriptors: one per concrete emitted key (built from a static per-pattern table in `metrics.py`: label English, unit, group, precision, min/max — e.g. voltage 0..30, current −10..10, power −300..300, pct 0..100, temp −40..125). `snapshot` returns descriptors for exactly the keys present in values.

- [ ] **Step 1: Failing tests** with fake trees under `tmp_path`: no sources → empty values, no exception; hwmon mapping via device path where `hwmon3` → `1-0041` yields `pwr.12v.*` (and renumbering to `hwmon7` still maps to 12v); unit conversion (12100 mV → 12.1, 1500 mA → 1.5, 18150000 µW → 18.15); window min/max/avg from three ticks (call internal `_sample_once()` directly, no real thread); unreadable `in1_input` → voltage key omitted, current still present (Review Focus 5); sampler tick raising → thread continues (inject failing source); CPU % from two `/proc/stat` snapshots; net deltas + excluded interfaces; swap absent → no key; descriptors cover exactly emitted keys and all keys pass `is_valid_key`; snapshot resets window.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Tests + ruff. **Step 5:** Commit `feat(agent): metrics collector with 1 Hz sampler + INA226 hwmon`.

---

### Task 8: Agent heartbeat wiring (metrics, descriptors, RTT)

**Files:**
- Modify: `station_agent/heartbeat.py`, `station_agent/agent.py`
- Test: `tests/test_agent_heartbeat_metrics.py`

**Interfaces:**
- `send_heartbeat(http_client, config=None, tx_settings=None, module_ota_backends=None, metrics=None)` where `metrics` is a `MetricsCollector` (or None → no metrics fields, current behaviour).
- `collect_system_info(..., metrics=None)` adds `metrics`, `metric_descriptors_hash` and, when needed, `metric_descriptors`. A small `DescriptorState` on the collector (`last_sent_hash`, `force_send` flag, lock) decides: send full list when `last_sent_hash is None` (first after start), hash changed, or `force_send` set by previous response `metric_descriptors_required: true`. Mark sent only after a 200 response.
- RTT: time the POST with `time.monotonic()`; on any HTTP response record `metrics.record_server_rtt(ms)` (reported in the next heartbeat).
- `agent.py`: create one `MetricsCollector()` at startup (after config load), `start()` it, pass it to both `send_heartbeat` call sites (main loop + `_heartbeat_keepalive`), `stop()` on shutdown. Collector failure to start must not stop the agent (log + `None`).
- Response parsing: reuse the already-parsed body (`tx_settings` path) — parse once even if `tx_settings` is None.

- [ ] **Step 1: Failing tests** (fake HttpClient recording payloads, fake collector): first heartbeat includes `metric_descriptors`; second with unchanged hash omits it but carries `metric_descriptors_hash`; changed hash → included; response `metric_descriptors_required: true` → next heartbeat includes list; failed POST (None/500) → list resent next time; RTT recorded; `metrics=None` → payload has no metrics keys; collector `snapshot` raising → heartbeat still sent without metrics; hash in payload == `descriptors_hash(list)`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** full suite + ruff. **Step 5:** Commit `feat(agent): send metrics + self-describing descriptors in heartbeat`.

---

### Task 9: Docs + contract memory

- [ ] Update repo `CLAUDE.md` (short "Generic metrics" section: contract module is shared, keys stable, how to add a metric = agent-only) and `station_agent/config.example.yml` only if a config knob was added (none planned).
- [ ] Commit `docs: generic metrics architecture notes`.

## Execution Order

Task 1 → then two parallel tracks: **Server** (2 → 3 → 4 → 5 → 6) and **Agent** (7 → 8). Task 9 last.
