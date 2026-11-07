# Repeater List & Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Global repeater DB (ÖVSV import + manual), a distance-sorted repeater panel that applies a repeater to the right FM module in one click, user-owned scan lists, and an agent-side sequential carrier scanner.

**Architecture:** New Django app `apps/repeaters` owns repeater data, ÖVSV sync, scan lists, station-side selection (bands/distance) and scan-list building. `apps/control` gains three WS message types (`apply_repeater`, `scan_start`, `scan_control`) and an `ActiveScan` row; the agent gains an `apply_preset` frame (batch of sets) and a `Scanner` asyncio task driven by a new FW telemetry capability `carrier`.

**Tech Stack:** Django 6.0, Channels, Alpine.js + server-rendered templates, Python asyncio agent, Zephyr C++ (FW), pytest, node-based JS tests.

**Spec:** `docs/superpowers/specs/2026-11-06-repeater-list-and-scanner-design.md` (read it before any task).

## Global Constraints

- Tests flat under `tests/test_*.py`; run `python -m pytest -q`; lint `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .`.
- Multi-line template comments only via `{% comment %}…{% endcomment %}` (CI template guard).
- Numeric inputs: dot-decimal forced (`lang="en"` on `<input type="number">`).
- All UI work invokes `Skill("frontend-design")` first.
- No new runtime dependency: outbound HTTP via `urllib.request` (pattern: `apps/images/github_releases.py`), mocked with `unittest.mock.patch`.
- ÖVSV mapping (verified): `frequency_tx → output_frequency` (we RX), `frequency_rx → input_frequency` (we TX), `ctcss_rx → input_tone`, `ctcss_tx → output_tone`; import only `fm = true`, bands `2m`/`70cm`. Attribution text: `Data: ÖVSV repeater database, CC BY 4.0` linking `https://repeater.oevsv.at/`.
- Defaults: listen window **2 s**, hang time **5 s**, carrier poll **100 ms**, radius **50 km**, sync interval **86400 s**.
- Tones are strings in FW format: `"none"`, CTCSS `"%.1f"` (e.g. `"77.0"`), DCS 3-digit code (e.g. `"023"`).
- Scan steps are never persisted, never written to `PersistedCapability`, never audited; only scan start/stop are `StationAuditLog` events.
- Slot identity: server side uses `StationModule.slot` (string); every frame sent to the agent carries it as `int` (as existing `command` frames do). Browser frames carry the string.
- FW work happens in repo `FW-RemoteStation` on its own branch/PR (Task 1); everything else in station-manager on `feature/repeater-list-scanner`.

## Review Focus

1. **Station without coordinates** → repeater panel still lists (callsign order, hint shown), radius source in scan start is disabled with message; never a 500. → tests in Task 4 and Task 9.
2. **Module goes offline / command errors mid-scan** → scanner skips that entry; if every entry of a full pass errors, scanner stops with `reason="no_reachable_entries"`. → test in Task 7.
3. **Effective scan list empty** (no repeaters in radius, lists only hold other bands) → `scan_start` refused with error `empty_scan_list`, no agent frame sent. → test in Task 9.
4. **Malformed ÖVSV rows** (missing site/coords, null ctcss, `frequency_tx == frequency_rx`, unknown band) → row imported with nulls where allowed, simplex where equal, skipped only if frequencies missing; one bad row never aborts the run. → test in Task 3.
5. **Lock changes hands while scanning** (transfer, preempt, release, holder-disconnect grace expiry) → server sends `scan_control stop`, `ActiveScan` deleted. → test in Task 9.

---

### Task 1: FW — `carrier` telemetry capability (repo FW-RemoteStation)

**Files:**
- Modify: `subsys/module/devices/sa818/sa818_module.cpp` (FieldSpec next to `BAND_SPEC` ~:168; new `CarrierCap : public Telemetry` next to `BandCap` ~:440; add `&g_carrier` to `g_caps[]` ~:717)
- Modify: `drivers/radio/sa818/sa818_shell.cpp` (fix usage string "squelch_sim" → "sim_squelch")
- Test: `tests/sim_shell/pytest/test_module_iface.py`

**Interfaces:**
- Produces: shell `module fm get carrier` → `MODULE-RESULT {ok:true, value:true|false}`; describe lists `{"name":"carrier","kind":"telemetry","type":"bool","readonly":true}`.

- [ ] **Step 1: Write failing tests** in `test_module_iface.py`: extend the capability-set assertion (~:40) with `"carrier"`; add `test_module_carrier_telemetry(sa818_sim, shell)`: `sa818 power on`; `sa818 sim_squelch closed` → `module fm get carrier` value `True`; `sa818 sim_squelch open` → value `False`. (Mark/skip like `test_sa818.py:141` where `CONFIG_GPIO_EMUL` is required.)
- [ ] **Step 2: Run** `west build -b native_sim tests/sim_shell && pytest tests/sim_shell/pytest/test_module_iface.py -k carrier` → FAIL (`unknown_capability`).
- [ ] **Step 3: Implement** `const FieldSpec CARRIER_SPEC{"carrier", ValueType::Bool, nullptr, nullptr, 0, nullptr, 0, /*readonly=*/true};` and `CarrierCap::onGet()`: not `ctx_.ready()` → `Result::err("driver_error")`; else `Result::okBool(sa818_get_squelch(ctx_.dev) == SA818_SQUELCH_CLOSED)` (CLOSED = carrier present — comment this inversion).
- [ ] **Step 4: Run** full `build_and_tests` + `clang_format` locally → PASS.
- [ ] **Step 5: Commit + PR** `feat(sa818): readonly carrier telemetry from SQL pin`. Note in PR: verify on HW whether SQL honours `rx_tone`.
- [ ] **Step 6: Release dry-run on the branch:** `gh workflow run release.yml -R OE5XRX/FW-RemoteStation --ref <branch> -f dry_run=true` → green (all release targets build + smoke).
- [ ] **Step 7: After the user merges the PR** (user gate): dispatch the real release on `main`: `gh workflow run release.yml -R OE5XRX/FW-RemoteStation --ref main -f dry_run=false` → new tag `YY.MM.DD-NN` (> `26.11.06-01`), release published with signed assets + `SHA256SUMS`.
- [ ] **Step 8: Verify the release:** `gh release view <tag> -R OE5XRX/FW-RemoteStation` lists the fm-sa818 assets incl. `.sig`/certs and `SHA256SUMS`.
- [ ] **Step 9: Roll to the FM module via prod** (confirm with the user before touching prod): import the release in prod station-manager (module-firmware GitHub release browser), assign it to the station's FM module, let the reconciler/OTA push it; confirm the module's inventory now advertises `carrier` and `module fm get carrier` answers. The station-manager tasks below do **not** wait for this — they test against `tests/fake_fw.py`.

---

### Task 2: `apps/repeaters` app — Repeater model, area, audit event

**Files:**
- Create: `apps/repeaters/{__init__,apps,models,admin}.py`, `apps/repeaters/migrations/0001_initial.py`
- Modify: `config/settings/base.py` (INSTALLED_APPS `"apps.repeaters"`), `apps/accounts/permissions.py` (`AREAS`), `apps/accounts/models.py` (`AccountAuditLog.EventType`) + migration, permissions docs table (regenerate via `manage.py permissions_table`)
- Test: `tests/test_repeaters_models.py`, existing `tests/test_permissions_registry.py` / `test_permission_matrix.py` / `test_permissions_doc.py` updated

**Interfaces:**
- Produces:
  - `Repeater(callsign:Char32, site_name:Char128 blank, locator:Char10 blank, output_frequency:Decimal(9,5), input_frequency:Decimal(9,5), input_tone:Char8 default "none", output_tone:Char8 default "none", bandwidth:Char4 blank ("12.5"/"25"/""), latitude/longitude:Decimal(9,6) null, source:Choices("oevsv","manual"), external_id:Char64 null unique, active:bool default True, replaces:FK("self", null, on_delete=SET_NULL, related_name="replaced_by"), comment:Text blank, last_synced_at:DateTime null, created_by:FK(User, null), created_at/updated_at)`
  - `Repeater.objects.visible()` → active and not replaced by an active manual entry.
  - `Repeater.is_simplex` property (`input == output`).
  - Area `"repeaters": Area(_("Repeaters"), view=A, manage=S)`.
  - `can_manage_repeaters(user) -> bool` in `apps/repeaters/permissions.py`: `can(user,"repeaters","manage")` or user has any `RegionAssignment` with `role=manager`.
  - `AccountAuditLog.EventType.REPEATER_CHANGED = "repeater_changed"`.

- [ ] **Step 1: Failing tests:** `test_visible_hides_inactive`, `test_visible_hides_oevsv_row_replaced_by_active_manual`, `test_visible_shows_oevsv_row_when_replacement_inactive`, `test_external_id_unique`, `test_can_manage_repeaters` (staff ✓, admin ✓, member ✗, member with region-manager assignment ✓, applicant ✗).
- [ ] **Step 2: Run** `python -m pytest tests/test_repeaters_models.py -q` → FAIL (import error).
- [ ] **Step 3: Implement** models, manager, admin (read-only for `source="oevsv"`), area, event type, migrations; update the three permission registry tests + docs table.
- [ ] **Step 4: Run** `python -m pytest -q tests/test_repeaters_models.py tests/test_permission*.py` → PASS.
- [ ] **Step 5: Commit** `feat(repeaters): repeater model, area and audit event`.

---

### Task 3: ÖVSV sync + `sync_repeaters` command + compose service

**Files:**
- Create: `apps/repeaters/oevsv.py`, `apps/repeaters/sync.py`, `apps/repeaters/management/commands/sync_repeaters.py`, `tests/fixtures/oevsv_trx.json`, `tests/fixtures/oevsv_site.json` (small recorded samples incl. OE5XLL rows and malformed rows)
- Modify: `docker-compose.yml`, `deploy/docker-compose.prod.yml` (service `repeater-sync`, copy of `alert-monitor` block with `command: python manage.py sync_repeaters --loop --interval 86400`)
- Test: `tests/test_repeaters_sync.py`

**Interfaces:**
- Produces:
  - `fetch_oevsv(timeout: float = 30.0) -> list[dict]` — GETs `https://repeater.oevsv.at/api/trx?fm=is.true&band=in.(2m,70cm)` and `/api/site`, joins on `site_name`, returns normalized dicts `{external_id, callsign, site_name, locator, output_frequency, input_frequency, input_tone, output_tone, latitude, longitude, active}`. Raises `OevsvError` on network/HTTP/JSON/schema failure.
  - `sync_repeaters(rows: list[dict], now: datetime) -> SyncResult(created:int, updated:int, deactivated:int, skipped:int)` — upsert by `external_id`, never touches `source="manual"`, rows missing from feed → `active=False`, `status=historic` → `active=False`, all in one transaction.
  - Command: `--loop`, `--interval` (default 86400); on `OevsvError` logs error, keeps data, continues loop.

- [ ] **Step 1: Failing tests:** `test_maps_oe5xll_2m` (output 145.600, input 145.000, tones "none", coords from site join); `test_ctcss_float_to_string` (77 → "77.0"); `test_equal_freqs_simplex`; `test_row_without_frequencies_skipped_others_imported` (Review Focus 4); `test_missing_site_gives_null_coords`; `test_upsert_idempotent` (second run: 0 created, 0 updated); `test_manual_rows_untouched`; `test_missing_from_feed_deactivated`; `test_historic_deactivated`; `test_fetch_error_keeps_data` (patch `apps.repeaters.oevsv.urllib.request.urlopen` raising `URLError` → command run leaves rows unchanged, logs error).
- [ ] **Step 2: Run** `python -m pytest tests/test_repeaters_sync.py -q` → FAIL.
- [ ] **Step 3: Implement** `oevsv.py`, `sync.py`, command, compose services.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(repeaters): nightly ÖVSV sync`.

---

### Task 4: Station-side selection (bands, distance, cross-band)

**Files:**
- Create: `apps/repeaters/selection.py`
- Test: `tests/test_repeaters_selection.py`

**Interfaces:**
- Consumes: `Repeater.objects.visible()` (Task 2); `StationModule` (`station.modules.filter(online=True)`, `capability_descriptor` list with `rx_frequency.ranges`).
- Produces:
  - `ModuleRef = NamedTuple(slot: str, module: str, band: str)`; band = range `name` from the module's `rx_frequency` capability.
  - `station_bands(station) -> dict[str, list[ModuleRef]]` — online modules that advertise `rx_frequency`, `tx_frequency`, `tx_tone`, `rx_tone`.
  - `band_of(freq: Decimal, bands: dict[str, list[ModuleRef]], station) -> str | None` — match against ranges.
  - `haversine_km(lat1, lon1, lat2, lon2) -> float`; `bearing_label(lat1, lon1, lat2, lon2) -> str` (8-point: N, NE, E, SE, S, SW, W, NW).
  - `RepeaterRow` dataclass: `repeater, distance_km: float|None, bearing: str|None, rx_band: str, tx_band: str, cross_band: bool`.
  - `repeaters_for_station(station, q: str = "", radius_km: float | None = None) -> list[RepeaterRow]` — both freqs in station bands; sorted by distance (stations without coords: by callsign, distance None, radius filter ignored); `q` case-insensitive on callsign/site_name.

- [ ] **Step 1: Failing tests:** `test_filters_to_station_bands` (2 m-only station hides 70 cm); `test_sorted_by_distance` with `bearing` for a known pair; `test_cross_band_flag_when_both_bands_present`; `test_cross_band_hidden_when_one_band_missing`; `test_no_coords_sorts_by_callsign_no_500` (Review Focus 1); `test_radius_filter`; `test_offline_module_ignored`; `test_search_q`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `selection.py` (pure Python, no PostGIS).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(repeaters): station band/distance selection`.

---

### Task 5: Manual repeater CRUD + list page

**Files:**
- Create: `apps/repeaters/{urls,views,forms}.py`, `apps/repeaters/templates/repeaters/{repeater_list,repeater_form,repeater_confirm_deactivate}.html`
- Modify: `config/urls.py` (`path("repeaters/", include("apps.repeaters.urls"))` in `i18n_patterns`), `templates/includes/sidebar.html` (`{% if can.repeaters.view %}`)
- Test: `tests/test_repeaters_views.py`

**Interfaces:**
- Consumes: `can_manage_repeaters` (Task 2).
- Produces: URL names `repeaters:list`, `repeaters:create`, `repeaters:update <pk>`, `repeaters:deactivate <pk>`. Create/update allowed only for `source="manual"`; form has optional `replaces` (ÖVSV rows only). Every write → `AccountAuditLog.log(event_type=REPEATER_CHANGED, actor=user, message=..., details={"repeater_id", "changes"})`. List shows source badge, attribution line, filter active/all.

- [ ] **Step 1: Failing tests:** view permission matrix (applicant view ✓ / create ✗; member create ✗; region-manager create ✓; staff create ✓); `test_update_oevsv_row_forbidden` (403); `test_create_audits`; `test_replaces_must_be_oevsv` (form error); `test_number_inputs_have_lang_en`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** views with `AreaPermissionMixin(area="repeaters")` for view; writes additionally check `can_manage_repeaters` (403 otherwise). Invoke `Skill("frontend-design")` for templates.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(repeaters): manual repeater management`.

---

### Task 6: Agent `apply_preset` frame

**Files:**
- Modify: `station_agent/broker.py` (`handle` routing + new method), `station_agent/protocol.py` (builder if needed)
- Test: `tests/test_broker_apply_preset.py`

**Interfaces:**
- Consumes: `Broker._execute(slot, module, "set", cap, token)`, `Broker.slot_busy(slot)`.
- Produces: frame in `{"v":1,"type":"apply_preset","request_id":str,"slot":int,"module":str,"values":{cap: value}}`; caps applied in fixed order `tx_frequency, rx_frequency, tx_tone, rx_tone, bandwidth` (only those present). Reply `build_result(request_id, ok=all_ok, value={cap: {"ok":bool, "error"?:{code,msg}}})` plus one `build_state(slot, module, {cap: value for ok caps}, ts)`. Refused with `error={"code":"ptt_active"}` when `slot_busy(slot)`. `async apply_preset(self, slot, module, values) -> dict[str, dict]` is public so the Scanner (Task 7) reuses it.

- [ ] **Step 1: Failing tests** (fake FW from `tests/fake_fw.py`): `test_applies_in_order` (fake records commands order); `test_partial_failure_reported_per_cap`; `test_refused_while_ptt`; `test_state_frame_contains_only_ok_caps`.
- [ ] **Step 2: Run** `python -m pytest tests/test_broker_apply_preset.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS (also `tests/test_broker*.py`).
- [ ] **Step 5: Commit** `feat(agent): apply_preset batch frame`.

---

### Task 7: Agent Scanner

**Files:**
- Create: `station_agent/scanner.py`
- Modify: `station_agent/broker.py` (route `scan_start`/`scan_update`/`scan_control`; hold on PTT key and on manual `set` of `frequency|rx_frequency|tx_frequency|rx_tone|tx_tone` for a scanned slot, also on `apply_preset` from a command; `stop()`/`on_disconnect()` stop the scanner; firmware drain on a scanned slot stops it), `tests/fake_fw.py` (settable `carrier`)
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `Broker.apply_preset` (Task 6); `Broker._execute(slot, module, "get", "carrier", None)`; router tx_route via `AudioRouterModule.handle_command("set","tx_route",{"slot":N,"module":m})` (virtual module slot 1000).
- Produces:
  - `Scanner(apply_preset, read_carrier, set_tx_route, send, *, sleep=asyncio.sleep, now=time.monotonic, poll_s=0.1)`
  - `start(scan_id, entries, listen_s, hang_s)`, `update(scan_id, entries)` (adopted at next step), `control(scan_id, action)` with `action in {"stop","skip","hold","resume"}`, `hold(reason)`, `stop(reason)`, `active_slots() -> set[int]`.
  - Entry dict: `{ref, label, slot, module, rx_frequency, tx_frequency, rx_tone, tx_tone}`.
  - Emits `{"v":1,"type":"scan_state","scan_id","state":"scanning|active|hang|held|stopped","index":int,"ref":str|None,"slot":int|None,"reason"?:str}` only on transitions.
  - Per-entry algorithm exactly as spec §7.3; on entering `active`/`held` sets tx_route to the entry's slot/module. Entry whose apply or carrier read errors is skipped; a full pass with only errors → `stop("no_reachable_entries")`.

- [ ] **Step 1: Failing tests** with fake clock/sleep and scripted carrier timeline: `test_no_carrier_advances_after_listen_window`; `test_short_over_inside_window_detected` (carrier true only at t=1.5 s); `test_hang_then_advance`; `test_carrier_returns_within_hang_stays_active`; `test_wraps_at_end`; `test_update_adopted_next_step`; `test_ptt_holds`; `test_manual_set_holds_then_applies`; `test_skip_hold_resume_stop`; `test_disconnect_stops` (broker `on_disconnect`); `test_all_entries_error_stops` (Review Focus 2); `test_tx_route_set_on_active`; `test_states_only_on_transition`.
- [ ] **Step 2: Run** `python -m pytest tests/test_scanner.py -q` → FAIL.
- [ ] **Step 3: Implement** `Scanner` as one asyncio task owned by `Broker`.
- [ ] **Step 4: Run** → PASS (plus all `tests/test_broker*.py`).
- [ ] **Step 5: Commit** `feat(agent): sequential carrier scanner`.

---

### Task 8: Server `apply_repeater` + repeater panel UI

**Files:**
- Modify: `apps/control/consumers.py` (`receive` chain: `"apply_repeater"` → `_handle_apply_repeater`; add to `_USE_ACTIONS`), `apps/control/views.py` (JSON endpoint), `apps/control/urls.py`, `apps/control/templates/control/panel.html` (include)
- Create: `apps/control/templates/control/_repeater_panel.html`, `static/js/repeater-panel.js`, `tests/js/repeater-panel.test.mjs`
- Test: `tests/test_control_apply_repeater.py`, `tests/test_control_repeater_endpoint.py`, `tests/test_repeater_panel_js.py` (node wrapper like `test_control_logic_js.py`)

**Interfaces:**
- Consumes: `repeaters_for_station`, `station_bands` (Task 4); agent `apply_preset` (Task 6).
- Produces:
  - Browser → server `{"type":"apply_repeater","request_id","repeater_id":int,"slot":str}`. Server: lock holder (`_touch_if_holder`) else `not_locked`; `can_write` for each of the four caps else `forbidden`; repeater visible + both freqs inside that slot's module ranges else `not_applicable`; cross-band → `not_supported`; relays `apply_preset` with `values={tx_frequency: input, rx_frequency: output, tx_tone: input_tone, rx_tone: output_tone, bandwidth?}`; registers the usual timeout in `self.pending`; `_audit` one entry "applied repeater <callsign>".
  - `GET stations/<pk>/control/repeaters/?q=` → `{"bands":{band:[{slot,module}]}, "station_has_coords":bool, "attribution":{...}, "repeaters":[{id,callsign,site_name,output_frequency,input_frequency,input_tone,output_tone,distance_km,bearing,rx_band,tx_band,cross_band}]}`; area `control` view.
  - JS: Alpine root `x-data="repeaterPanel"`; sends via `window.dispatchEvent(new CustomEvent("control:send", {detail: frame}))` — `controlPanel()` gets a listener forwarding `detail` to `_send`. Band→slot choice stored in `localStorage["oe5xrx.bandmap.<station_id>"]`; dropdown only when a band has >1 module. Highlights the row matching current `rx_frequency`/`tx_frequency` from `_onState`. Cross-band rows marked "cross-band – not yet supported", not clickable.

- [ ] **Step 1: Failing tests:** consumer: `test_apply_requires_lock`, `test_apply_relays_preset_values` (OE5XLL → tx 145.000 / rx 145.600), `test_apply_rejects_out_of_range`, `test_apply_rejects_cross_band`, `test_apply_forbidden_without_write`; endpoint: shape, `station_has_coords=False` path; JS: bandmap persistence, dropdown visibility, cross-band not clickable.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (`Skill("frontend-design")` first for the panel).
- [ ] **Step 4: Run** `python -m pytest -q tests/test_control_*.py tests/test_repeater_panel_js.py` → PASS.
- [ ] **Step 5: Commit** `feat(control): repeater panel and apply_repeater`.

---

### Task 9: Scan lists, scan builder, server scan orchestration

> **Split for parallel execution:** **9a** = everything in `apps/repeaters` (ScanList/ScanListEntry models + migration `0002`, owner-only CRUD + `mine.json`, `build_scan_entries`, their tests `tests/test_repeaters_scanlists.py`, `tests/test_scan_builder.py`). **9b** = everything in `apps/control` (`ActiveScan` + migration, consumer frames, lock hooks, list-edit → `scan_update` signal, `tests/test_control_scan.py`). 9a runs parallel to Task 8; 9b starts after Task 8 and 9a are merged (both touch `consumers.py`).

**Files:**
- Modify: `apps/repeaters/models.py` (+ migration), `apps/repeaters/{views,urls,forms}.py`, templates `apps/repeaters/templates/repeaters/scanlist_*.html`
- Create: `apps/repeaters/scan.py`, `apps/control/scan.py`; migration in `apps/control` for `ActiveScan`
- Modify: `apps/control/consumers.py` (`scan_start`, `scan_control`, agent `scan_state` broadcast), `apps/control/lock.py` (stop hook on release/transfer/preempt/disconnect-grace expiry; sweep skip), `apps/control/models.py`
- Test: `tests/test_repeaters_scanlists.py`, `tests/test_scan_builder.py`, `tests/test_control_scan.py`

**Interfaces:**
- Produces:
  - `ScanList(owner FK User, name Char64, created_at)`, `ScanListEntry(scan_list FK related_name="entries", position int, repeater FK null, frequency Decimal(9,5) null, tone Char8 default "none", label Char64 blank)`; exactly one of `repeater`/`frequency` set (model `clean` + DB `CheckConstraint`). CRUD under `repeaters/scanlists/`, owner-only, requires `can(user,"control","use")`.
  - `build_scan_entries(station, user, *, radius_km: float|None, list_ids: list[int], band_map: dict[str,str]) -> list[dict]` (`apps/repeaters/scan.py`): own lists in given order then radius repeaters by distance; dedupe on `(rx_frequency, rx_tone)`; skip entries outside station bands or cross-band; resolve `slot/module` via `band_map` else the single module of that band; free frequency → simplex, `rx_tone="none"`, `tx_tone=tone`; `ref` = `"r:<id>"` or `"f:<entry_id>"`. Raises `NoCoordinates` if `radius_km` set and station lacks coords.
  - `ActiveScan(station OneToOne, user FK, scan_id Char32, list_ids JSON, radius_km float null, band_map JSON, listen_s float, hang_s float, started_at)`.
  - Browser frames: `{"type":"scan_start","request_id","radius_km":float|null,"list_ids":[int],"band_map":{band:slot},"listen_s":2,"hang_s":5}` and `{"type":"scan_control","request_id","action":"stop|skip|hold|resume"}` (both in `_USE_ACTIONS`, lock holder only). `scan_start` errors: `not_locked`, `forbidden` (no write on freq/tone caps), `no_coordinates`, `empty_scan_list` (no agent frame sent). Success → `ActiveScan` saved, `scan_start` relayed with `scan_id=uuid4().hex`, `StationAuditLog` "scan started".
  - `scan_state` from agent broadcast to browsers like `state`; `state=="stopped"` deletes `ActiveScan` + audit "scan stopped (<reason>)".
  - `ScanListEntry`/`ScanList` save/delete signal → for each `ActiveScan` whose `list_ids` contain it: rebuild and group_send `control.to_agent` `scan_update`.
  - `stop_active_scan(station, reason)` in `apps/control/scan.py`: deletes row + sends `scan_control stop`; called from lock release/transfer/preempt/grace expiry. `sweep_lock` does not idle-release while an `ActiveScan` exists and `pending_release_at is None`.

- [ ] **Step 1: Failing tests:** scan lists: owner-only CRUD, one-of constraint; builder: order, dedupe, band skip, band_map resolution, `NoCoordinates`; consumer: `test_scan_start_relays_entries`, `test_scan_start_empty_list_refused` (Review Focus 3), `test_scan_start_no_coords_radius_refused` (Review Focus 1), `test_scan_control_requires_holder`, `test_list_edit_pushes_scan_update`, `test_lock_transfer_stops_scan` + preempt + release + grace expiry (Review Focus 5), `test_idle_sweep_skipped_while_scanning`, `test_stopped_state_deletes_active_scan`, `test_scan_steps_not_persisted` (no `PersistedCapability` rows after state frames).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m pytest -q tests/test_scan*.py tests/test_control_*.py tests/test_repeaters_*.py` → PASS.
- [ ] **Step 5: Commit** `feat: scan lists and server scan orchestration`.

---

### Task 10: Scanner UI

**Files:**
- Create: `apps/control/templates/control/_scanner_panel.html`, `static/js/scanner-panel.js`, `tests/js/scanner-panel.test.mjs`
- Modify: `apps/control/templates/control/panel.html`, `static/js/control-panel.js` (`_route` forwards `scan_state` as `window` event `control:scan_state`)
- Test: `tests/test_scanner_panel_js.py`

**Interfaces:**
- Consumes: frames from Task 9; `control:send` event (Task 8); bandmap localStorage key (Task 8); scan lists JSON (add `GET repeaters/scanlists/mine.json` → `[{id,name,count}]` in Task 9's views if not yet there).
- Produces: panel hidden unless some online module advertises `carrier`; start form (radius checkbox + km, own lists multi-select, listen/hang inputs with `lang="en"`); state line (state, label, frequency, module, `index+1 / n`); buttons stop/skip/hold/resume; on scan start subscribes RX audio of all slots in the scan (via the existing audio panel subscribe path, builder picks the hook).

- [ ] **Step 1: Failing JS tests:** hidden without `carrier`; start frame shape; state rendering per state; buttons send `scan_control` actions.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (`Skill("frontend-design")` first).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(control): scanner panel`.

---

### Task 11: E2E + docs

**Files:**
- Create: `tests/test_e2e_repeater_scan.py`
- Modify: `CLAUDE.md` (short section "Repeater & Scanner": ÖVSV direction mapping, scan-steps-not-persisted invariant, agent owns loop / server owns list), `deploy/` notes if the servers repo needs the `repeater-sync` service (mention in PR body)

- [ ] **Step 1: Write E2E test** (probe scope): browser communicator acquires lock → `apply_repeater` OE5XLL → agent communicator receives `apply_preset` with tx 145.000/rx 145.600 → agent result → browser gets result. Then `scan_start` with one own list (2 entries) → agent receives `scan_start` → agent sends `scan_state active` → browser receives it → agent `stopped` → `ActiveScan` gone.
- [ ] **Step 2: Run** `python -m pytest tests/test_e2e_repeater_scan.py -q` → PASS (fix forward if not).
- [ ] **Step 3: Full suite + lint:** `python -m pytest -q && uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .` → all green.
- [ ] **Step 4: Commit** `test: repeater/scan E2E; docs`.
