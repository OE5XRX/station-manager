# Repeater List & Scanner — Design

**Date:** 2026-11-06 · **Status:** draft for review · **Repos:** station-manager (server + `station_agent`), FW-RemoteStation (one small capability)

Decisions marked **(call)** were made by the designer, not explicitly by the user — review them first.

---

## 1. Intent

An operator controlling a remote station over the web wants two things in the station control view:

1. **Repeater list** — "which repeaters can I work from *this* station?" Pick one, and the FM module is set up for it (frequencies, tone) in one click instead of typing split + CTCSS by hand.
2. **Scanner** — let the station listen around by itself: step through repeaters and calling frequencies, stop when someone is talking, move on when it's quiet again. Classic handheld scan, but driven from the web.

Success: at a station you open the control view, see the nearest repeaters first, click one and you're on it; or tick "repeaters within 50 km" + "my calling frequencies", press scan, and hear activity as it happens.

## 2. Goals / Non-goals / Anti-goals

**Goals**
- Global repeater database in station-manager, auto-imported nightly from the ÖVSV repeater DB, plus manual entries.
- Repeater panel in station control, filtered to the bands the station's modules can do, sorted by distance from the station.
- One click applies a repeater to the module that has the matching band.
- User-owned scan lists that travel with the user across stations.
- Scanner: sequential scan over a combined list, carrier-triggered stop, hang time, runs in the agent.

**Non-goals (this iteration)**
- Cross-band repeat operation (RX on module A, TX on module B). The **data model and band→module routing must allow it** (see §5.4); the operation itself comes later. The audio side already supports it (`tx_route` on the `audio-router` virtual module, cross-band link in the audio spec).
- Station-shared scan lists (may come later if operation shows the need).
- Other repeater sources (repeatermap.de, RepeaterBook, hearham, dl3el). Neighbouring-country repeaters go in manually.
- Exposing repeaters/scan lists through the user automation API (`/api/v1`). Later extension.
- Parallel scanning on several modules.
- Digital modes (DMR/C4FM/D-Star). Import FM only.

**Anti-goals (would make this a failure even if it works)**
- The scan loop running in the browser or on the server, stepping each channel over the network — fragile on a weak uplink, misses the start of transmissions.
- The scan list living on the agent and needing to be edited there. **The list's source of truth is station-manager.**
- Every scan step written to the DB / audit log / persisted-capability store (flooding).
- Silent repeater-data drift: manual entries overwritten by the importer.

## 3. Constraints & existing facts (verified against `origin/main` @ a430b51)

- **Module control path:** browser sends `{type:"command", request_id, slot, module, capability, op, value}` over `ws/control/<station_id>/` → `apps/control/consumers.py` `ControlConsumer` (requires lock holder, `capability_policy.can_write`) → relayed to agent → `station_agent/broker.py` `handle_command` → `slot_control.py` writes `module <id> <op> <cap> [value]` to the FM board shell. **One capability per command; no batch message exists.**
- **FM module capabilities** (FW `subsys/module/devices/sa818/sa818_module.cpp`): `frequency` (simplex), `tx_frequency`, `rx_frequency`, `tx_tone`, `rx_tone` (`none` / CTCSS Hz / DCS code), `squelch`, `bandwidth`, `power_level`, `volume`, `rssi` (raw, readonly), `band` (readonly). Ranges are advertised per capability as named bands (e.g. `vhf` 134–174).
- **Carrier detect:** the SA818 driver already reads the SQL pin (`sa818_get_squelch`; LOW = carrier present). It is **not** exposed as a module capability yet.
- **SA818 UART** 9600 baud; `AT+DMOSETGROUP` sets bw, tx/rx freq, tx/rx tone, squelch in one command. Each frequency change ≈ 70 ms on the wire + module settling (unmeasured).
- **Lock:** `ControlLock` (`apps/control/lock.py`), user-owned; idle release after 300 s without activity; browser disconnect → 12 s grace → release; agent disconnect → force free.
- **Inventory:** agent reports slots/modules/capability descriptors; stored in `StationModule.capability_descriptor` / `last_state`.
- **Agent → GUI:** `state`, `event`, `result`, `inventory` frames, broadcast to `control_<id>` group.
- **Background jobs:** no Celery. Management commands with `--loop --interval` run as their own compose service (`check_alerts`, `history_maintain`, `run_background_jobs` …).
- **Permissions** (`apps/accounts/permissions.py`): membership levels applicant < member < staff < admin; `can(user, "control", "use")` = member; region managers via `RegionAssignment.Role.manager`.
- **Station coordinates:** `Station.latitude` / `Station.longitude` (nullable decimals).
- Repo conventions: tests top-level `tests/test_*.py`, ruff, `{% comment %}` for multi-line template comments, `lang="en"` / dot-decimal on numeric inputs (de_AT locale), frontend work via `frontend-design` skill.

## 4. Repeater database

### 4.1 Data
One row per repeater **frequency pair** (a site with 2 m and 70 cm = two rows). Fields:

| Field | Notes |
|---|---|
| callsign, name/site name, QTH/locator | display |
| `output_frequency` | what the repeater transmits = what **we receive** (MHz) |
| `input_frequency` | what the repeater listens on = what **we transmit** (MHz). Simplex/no-split entries: equal to output |
| `input_tone` | tone we must send (CTCSS/DCS/none) |
| `output_tone` | tone the repeater sends, if any (used as our `rx_tone`) |
| bandwidth | optional (12.5 / 25 kHz) — set only if known |
| latitude, longitude | required for distance; entries without coordinates are listed last |
| `source` | `oevsv` / `manual` |
| `external_id` | ÖVSV `uid` for upsert; null for manual |
| `active` | false for ÖVSV `status=historic` or entries gone from the feed |
| comment, last_synced_at | |

Band is **derived** from each frequency against the module's advertised ranges, not stored as authoritative. An entry is cross-band when input and output fall into different bands.

### 4.2 ÖVSV import
- Source: `https://repeater.oevsv.at/api/trx` (PostgREST, no auth) joined with `/api/site` on `site_name` for coordinates. Filter FM, 2 m + 70 cm. License **CC BY 4.0** → the repeater panel shows "Data: ÖVSV repeater database, CC BY 4.0" with link.
- Upsert by `external_id`. Never touch `source=manual` rows. Feed rows with `status=historic`, or rows no longer in the feed → `active=false` (soft, never hard-delete).
- Runs nightly as a management command (`sync_repeaters --loop --interval 86400`) in its own compose service, following the existing pattern **(call)**. A staff-only "sync now" button is nice-to-have.
- **Direction semantics — verified** against OE5XLL 2 m (user-confirmed: repeater transmits 145.600, listens 145.000; API row `frequency_tx=145.6, frequency_rx=145`): ÖVSV fields are from the **repeater's perspective**. Mapping: `frequency_tx → output_frequency` (our RX), `frequency_rx → input_frequency` (our TX), and by the same convention `ctcss_rx → input_tone` (tone we send), `ctcss_tx → output_tone`. The tone mapping is inferred by analogy — spot-check one toned repeater during implementation. Import only rows with `fm = true` (some rows have `fm = null`). The `site_name` join is inferred from the schema; a `trx_list` view with coordinates exists but is permission-denied.
- Import failures (network, schema change) must not wipe data: abort the run, keep the last good state, log loudly.

### 4.3 Manual entries
- Create/edit/deactivate manual entries: **staff, plus any user holding a region-manager assignment (any region) — repeaters have no region link (call)**. Viewing: everyone who can view station control.
- **ÖVSV rows are read-only** in station-manager (the nightly sync owns them) **(call)**. To correct one (wrong tone, missing coordinates), create a manual entry with an optional "replaces ÖVSV entry" link; a replaced ÖVSV row is hidden from panel and scans as long as the manual entry exists. This keeps the sync from silently undoing corrections and avoids duplicates in panel and scan dedup.
- Manual edits are audit-logged (who/what/when) using the existing audit mechanism.

## 5. Repeater panel in station control

### 5.1 Placement
Its own panel in the station control view, **next to the module panels**, not inside a module card (user decision — a repeater may span modules).

### 5.2 Filtering & sorting
- Show only active repeaters whose input **and** output frequency fall into bands that some online module of this station advertises.
- Sorted by distance from the station (haversine on lat/lon, computed in Python — ~130 rows, no PostGIS needed **(call)**). Show distance and bearing, e.g. "23 km NE".
- Station without coordinates: list sorted by callsign, with a hint that distance needs station coordinates.
- Simple text search over callsign/site is cheap and expected **(call)**.

### 5.3 Applying a repeater
- Click → settings go to the module that has the matching band: `rx_frequency = output`, `tx_frequency = input`, `tx_tone = input_tone`, `rx_tone = output_tone or none`, `bandwidth` only if the entry has one. Squelch/power/volume stay as they are.
- Requires holding the lock and the same per-capability write permission as setting them by hand. Refused while PTT is active **(call)**.
- Server-side resolution **(call)**: the browser sends one message referencing the repeater id (e.g. `{type:"apply_repeater", repeater_id, slot}`); the server resolves the values, relays the individual `set` commands to the agent sequentially, and returns one aggregated result. Partial failure is reported per capability (no rollback). "Persist" means exactly what a manual set does today: frequency/tone are `persist=False` in `capability_policy`, so only `last_state` updates — no policy change **(call)**.
- The panel highlights the repeater the module is currently tuned to (match on current `rx_frequency`/`tx_frequency`).

### 5.4 Several modules on the same band
- Band→module mapping: if a band is served by **more than one** module, the repeater panel shows a dropdown per such band ("2 m → Module A"). Hidden when each band has exactly one module.
- The choice is remembered per user and station across page loads (browser-local storage is fine) **(call)**. Clicks then go straight to the chosen module — no per-click prompt.
- Cross-band (later): the same mapping yields "RX on the output-band module, TX on the input-band module, `tx_route` to the TX module". **MVP:** cross-band entries are listed when the station has both bands but marked "cross-band – not yet supported" and not clickable **(call)**.

## 6. Scan lists

- **Own lists:** owned by a user, named, ordered entries. An entry is either a reference to a repeater, or a free frequency (simplex, optional tone, label). Lists go with the user to every station. Any user who can operate (`control.use`) can create their own lists.
- **Radius source (automatic, no curation):** "repeaters within X km of this station", built from the repeater DB + station coordinates + module bands. Default radius 50 km **(call)**, adjustable at scan start.
- At a station, entries whose band the station lacks are skipped silently; far-away repeaters in own lists show their distance so the user sees they are probably out of reach.

## 7. Scanner

### 7.1 Starting a scan
The user ticks sources — the radius source and/or one or more own lists — and presses scan. The **server** builds the effective list: union, de-duplicated (same rx freq + rx tone = same entry), filtered to the station's bands, each entry resolved to a concrete `slot`/`module` via the band→module mapping (§5.4). Order: own lists in their order first, then radius repeaters by distance **(call)**.

### 7.2 Where it runs
- **List lives on station-manager** (source of truth, editable any time).
- **Loop runs in the station agent.** Server sends the resolved list on start; on any list edit during a running scan the server pushes the new resolved list, the agent adopts it at the next step. If the agent's server connection drops, the agent **stops the scan** and leaves the module on the current entry (dead-man, like PTT) **(call)**. Reason: the server force-frees the lock on agent disconnect, and the operator can't hear anything anyway (audio goes via the server) — a scan continuing ownerless would only fight the next operator.
- **Strictly sequential**, one entry at a time, across modules — consecutive entries may be on different modules.

### 7.3 Per-entry algorithm
1. Set the entry's `rx_frequency`, `tx_frequency`, `rx_tone`, `tx_tone` on its module.
2. **Listen window** (default 2 s): poll `carrier` continuously (≈ every 100 ms — not a single sample at the end, so short overs aren't missed). No carrier → next entry.
3. Carrier → **active**: stay, operator hears the traffic.
4. Carrier drops → **hang time** (default 5 s). Carrier returns within it → stay active. Otherwise → next entry.
5. End of list → start over.

Listen window and hang time are adjustable per scan. Settling time after a frequency change is irrelevant at second-scale windows; no bench measurement needed.

### 7.4 Operator interaction
- Stop, skip current entry, hold (stay on current entry indefinitely), resume.
- **PTT while scanning:** scan goes to *held* on the current entry, transmitting on that entry's input frequency/tone; the user resumes manually **(call)**.
- **Manual change on a scanned module while scanning** (a hand `set` of frequency/tone, Apply repeater): the scan goes to *held* first, then the change is applied — same rule as PTT **(call)**. A module-firmware drain/OTA on a scanned slot stops the scan.
- While active/held, `tx_route` points at the current entry's module so PTT keys the right module.
- GUI shows scan state (scanning / active / hang / held / stopped), current entry (label, frequency, which module), and position in the list.
- The operator must hear whichever module the scan is on: the GUI subscribes to the RX streams of all modules the scan uses (builder's choice how).

### 7.5 Lifecycle & permissions
- Scanning is part of the operating session: start requires holding the lock and write permission for the frequency/tone capabilities.
- Lock released (manual, transfer, preempt, browser-disconnect grace) → server sends stop. Agent disconnect → agent stops on its own (§7.2).
- A running scan counts as lock activity while the holder's browser is connected, so the 300 s idle release doesn't kill an unattended-but-watched scan **(call)**.
- On stop the module stays on the entry it was last on (reflected in `last_state`, see §5.3). Individual scan steps are **not** persisted, audited, or written to `PersistedCapability`; only start/stop are audit events.

### 7.6 Protocol additions (shape is builder's choice; intent below)
- server → agent: `scan_start {scan_id, entries[{ref, label, slot, module, rx_frequency, tx_frequency, rx_tone, tx_tone}], listen_s, hang_s}`, `scan_update {scan_id, entries}`, `scan_control {scan_id, action: stop|skip|hold|resume}`.
- agent → server → browsers: `scan_state {scan_id, state, index, ref, slot, ts}` on every transition (not every poll).
- Scan steps bypass the per-command server round-trip; the agent drives `slot_control` directly.

### 7.7 Firmware addition (FW-RemoteStation, separate small PR)
- New readonly telemetry capability `carrier` (bool) on the SA818 module, backed by the SQL pin (`true` = signal present). Sim + tests as for existing capabilities.
- **To verify** on real hardware when available: whether the SQL pin honours the configured `rx_tone` (CTCSS squelch). If yes, the scanner only stops when *that* repeater is talking; if not, it stops on any carrier on the frequency — acceptable either way.
- Scanner UI is hidden for modules whose descriptor lacks `carrier` (older firmware).

## 8. Open for the builder
- Django app layout (suggestion: new `apps/repeaters` for repeater data + import + scan lists; scan orchestration in `apps/control`).
- Exact message names/shapes, UI layout details (via `frontend-design`), polling interval tuning, test structure.
- How the RX-stream subscription follows the scan.

## 9. Testing expectations
- Importer: fixture-based (recorded ÖVSV responses), upsert idempotency, manual rows untouched, historic → inactive, failed fetch keeps data.
- Filtering/sorting: bands × modules, missing coordinates, cross-band marking, multi-module dropdown mapping.
- Apply: permission/lock/PTT refusal, per-capability partial failure.
- Agent scanner: unit tests with a fake slot control + simulated carrier timeline (listen window, hang, return-within-hang, end-of-list wrap, list update mid-scan, PTT → held, manual set → held, server disconnect → scan stops).
- E2E (probe): browser start scan → server builds list → agent scans sim module → carrier event in sim → GUI shows active → hang → next entry.
