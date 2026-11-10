# Passive Services & APRS RX-iGate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Idle stations switch on their own to passive APRS receive. Packets are decoded on the station by Direwolf, stored and mapped in station-manager, and gated to APRS-IS as one RX-iGate login per station.

**Architecture:** The control lock gets one hook pair, `on_lock_released`/`on_lock_acquired`, which every lock path calls. A per-agent-connection `PassiveSupervisor` lives in `AgentControlConsumer` and is driven by the existing 5 s sweep. It tunes the radio with system-issued `command` frames, starts and stops decoders with new `decoder_*` frames, and ingests `decoder_output`. The agent gets a generic `DecoderHost`: systemd template instances `oe5xrx-decoder@<decoder>-slot<N>`, KISS reading, and a bounded RAM spool with seq/ack. A separate `manage.py aprs_igate` worker gates fresh packets from the DB to APRS-IS and sends beacons.

**Tech Stack:** Django 6.0, Channels, DRF-free JSON views, PostgreSQL/SQLite, asyncio (agent, `websockets`), systemd, Direwolf 1.8.1 (image plan), Leaflet 1.9.4 (vendored), plain JS + `node --test` for JS logic.

**Spec:** `docs/superpowers/specs/2026-11-10-passive-services-aprs-design.md`. Companion plan for linux-image and servers: `docs/superpowers/plans/2026-11-10-passive-services-aprs-image.md`.

## Global Constraints

- One feature branch `feature/passive-services-aprs` and **one PR** at the end (squash-merge). No doc-only interim PRs.
- **Prerequisite:** PR #196 (`fix/agent-state-readback`, adds `Broker._settings_snapshot`) must be merged before Task 10. Rebase this branch onto `main` once it lands. Do **not** re-implement the read-back. Tasks 1–9 do not depend on it.
- Tests are flat `tests/test_*.py`. Run with `python -m pytest -q`. Lint with `ruff check . && ruff format --check .` (line length 99).
- Agent tests use **no pytest-asyncio**: wrap scenarios in `asyncio.run(...)` and use the `Collector` send fake pattern from `tests/test_broker.py:58-63`.
- Consumer tests use `channels.testing.WebsocketCommunicator`, `@pytest.mark.django_db(transaction=True)` and the `control_agent_auth` fixture (`tests/conftest.py:295`), following `tests/test_control_consumer_lock.py`.
- Spec defaults, verbatim:
  - APRS profile: 144.800 MHz, squelch 0, no CTCSS/tones, wide bandwidth.
  - Hand-over profile: 145.500 MHz, squelch 1, no tones.
  - Raw packet retention: 30 days.
  - `max_gate_age`: 30 s.
  - Beacon: every 30 min, symbol `R&`, comment "OE5XRX Remote Station RX-iGate".
  - APRS-IS: `rotate.aprs2.net:14580`.
  - `no_rx_alert_hours`: 6.
  - Spool: a few thousand packets or about 1 MB. This plan uses 2000 packets / 1 000 000 bytes.
  - Coverage cell: about 2 km.
  - iGate SSID form hint: 10.
- Frequencies are **MHz decimals**, as in `apps/repeaters` (`float(repeater.output_frequency)`, `apps/control/consumers.py:738-739`). Capability names: `frequency`, `rx_frequency`, `tx_frequency`, `rx_tone`, `tx_tone` (value `"none"` = no tone), `squelch`, `bandwidth` (`"25"` = wide, `"12.5"` = narrow), `volume`.
- `filter_*` caps are never part of any profile. System writes are never persisted (`apps/control/persistence.py` is never called for them).
- Audit actor for system actions: `user=None` with a message prefixed `system:passive_services` (precedent: `apps/control/consumers.py:523-526`).
- Templates: `{% comment %}` only, never multi-line `{# #}`. `lang="en"` on every `<input type="number">`. `Skill("frontend-design")` is **mandatory** for Tasks 15–17.
- Agent package list is explicit (`station_agent/pyproject.toml` `packages=[...]`). A new subpackage must be added there.
- Permissions: new area `passive_services` (view = Applicant, manage = Admin). `docs/permissions.md` must match `manage.py permissions_table` (`tests/test_permissions_doc.py`).

## Decisions taken for spec §12 ("open for the builder")

| Topic | Decision |
|---|---|
| App | New Django app `apps.passive` (label `passive`). APRS-specific code in `apps/passive/aprs/`. |
| Scheduler placement | `PassiveSupervisor`, one per `AgentControlConsumer`, ticked from `_sweep_loop` (`apps/control/consumers.py:342-365`). The start sequence runs in the sweep task, never in `receive`, because it awaits `result` frames that arrive through `receive`. Back-off: 60 s, 120 s, 240 s … capped at 900 s, reset on a successful start. |
| Idle timer | New `ControlLock.released_at`, set in `lock._clear` (`apps/control/lock.py:266`), which every free path goes through. `NULL` (never held) counts as idle. |
| Frames | ↓ `decoder_start{decoder,slot,params,request_id,max_runtime_s}`, ↓ `decoder_stop{decoder,slot,request_id}`, ↓ `decoder_ack{epoch,seqs}`. ↑ `decoder_state{decoder,slot,state,detail}`, ↑ `decoder_snapshot{decoders:[{decoder,slot,state}]}` (always sent on connect, may be empty), ↑ `decoder_output{decoder,slot,epoch,seq,kind,reliable,data,ts}`. Start and stop are acked with the existing `result` frame. Feature advertisement: `inventory` gets top-level `"features": ["decoders"]`. |
| Decoders across a WS outage | Decoders **keep running** while the control WS is down. The spool exists for HAMNET drops. The `DecoderHost` lives in `ControlClient` (process lifetime), not in the per-connection `Broker`. "Agent offline → nothing runs" (spec §4.6) means the server starts nothing for a station whose agent is not connected. On agent **process** start every leftover `oe5xrx-decoder@*` unit is stopped. |
| RX node | The agent resolves it (`PipeWireRouterBackend.resolve_node(slot,"rx")`, `station_agent/audio/router_backend.py:85`) and writes it into the env file. The image wrapper only consumes `OE5XRX_RX_NODE`. Nothing else knows the hardware node naming. |
| Clock skew | The server computes `heard_at = recv_now - clamp(ts - data.heard_at, 0, 7 days)`. Only agent-clock *differences* are trusted, never the absolute agent clock. |
| APRS parsing | Own small parser (`aprslib` 0.7.2 is unmaintained since 2022-07). Supports AX.25 → TNC2, uncompressed and compressed positions, Mic-E, objects/items, status, message and third-party. Everything else becomes type `other`. |
| Coverage grid | Simple lat/lon grid of 0.02° × 0.02° (≈ 2.2 km × 1.5 km at 47° N). Key `(cell_lat, cell_lon) = (floor(lat*50), floor(lon*50))`. |
| Live log | JSON polling every 5 s (`?after_id=`), not a new WS consumer. The control page gets the passive **state** live over the existing control WS. |
| Map | Leaflet 1.9.4 vendored in `static/vendor/leaflet/` (pattern: `static/vendor/uplot/`). OSM tiles. `https://tile.openstreetmap.org` is added to `img-src` in `SECURE_CSP` (`config/settings/base.py:277-285`). |
| Retention | Done by the `aprs_igate` worker's housekeeping, once per 24 h. No extra compose service. |
| RX alert | New `AlertRule.AlertType.PASSIVE_NO_RX = "passive_no_rx"` checked in `apps/monitoring/engine.py` (runs in the existing `check_alerts --loop`). |
| Beacon tocall | `APZXRX` (APZ = experimental block). Registering a real tocall is a follow-up. |

## Review Focus

1. **Agent process restart with a decoder still running.** systemd units outlive the agent. Expect: the agent stops every `oe5xrx-decoder@*` unit at start and reports an empty `decoder_snapshot`. The server closes the run and re-schedules. Pinned by `test_host_stops_leftover_units_on_startup` (Task 5) and `test_snapshot_empty_closes_open_run` (Task 10).
2. **A user grabs the lock while the passive start sequence is half-way** (profile commands in flight, `decoder_start` not sent yet). Expect: no decoder starts. The hand-over profile is applied once and the run ends with `end_reason="preempted"`. Pinned by `test_preempt_during_start_aborts_before_decoder_start` (Task 10).
3. **Drift re-apply overwriting the passive `volume`.** The inventory and `_retry_pending_reapply` paths re-send persisted `volume` while APRS runs. Expect: caps in the active passive profile on that slot/module are excluded from re-apply while a run is open. Pinned by `test_reapply_skips_passive_profile_caps` (Task 10).
4. **Station clock wrong by minutes, or packets resent after a long HAMNET outage.** Expect: freshness is judged on the server clock via the skew-free `heard_at`. Resent old packets are stored but never gated. Pinned by `test_heard_at_uses_agent_clock_delta_only` (Task 11) and `test_stale_packet_marked_not_gated` (Task 13).
5. **Two stations with the same `igate_callsign` + SSID, or a call typed lowercase or with spaces.** Expect: the form normalises to upper case and rejects duplicates and invalid formats. One login per distinct `CALL-SSID`. Pinned by `test_igate_call_unique_and_normalised` (Task 7).

---

## File Structure

**Agent (`station_agent/`)**
- `protocol.py` (modify): new frame builders; `build_inventory(slots, features=None)`.
- `decoders/__init__.py` (create): package marker; `FEATURE = "decoders"`.
- `decoders/kiss.py` (create): KISS de-framing.
- `decoders/spool.py` (create): bounded reliable-output spool with epoch/seq/ack.
- `decoders/units.py` (create): systemd template-unit control and env-file writing.
- `decoders/aprs.py` (create): APRS plugin (env, KISS reader → outputs).
- `decoders/host.py` (create): `DecoderHost`, the lifecycle, state frames, max_runtime, health polling, slot reconcile.
- `broker.py` (modify): dispatch `decoder_start`/`decoder_stop`/`decoder_ack` to an injected host.
- `control_client.py` (modify): own one `DecoderHost`; attach/detach per connection; snapshot + resend; reconcile on inventory.
- `agent.py` (modify): build the host with the RX-node resolver; stop leftovers on start.
- `pyproject.toml` (modify): add `station_agent.decoders`.

**Server**
- `apps/control/lock_hooks.py` (create): the hook pair + hook registry.
- `apps/control/consumers.py` (modify): all lock paths call the hooks; supervisor wiring; new frame types.
- `apps/control/lock.py`, `apps/control/models.py` (modify) + migration `0012_controllock_released_at.py`.
- `apps/passive/` (create): `apps.py`, `models.py`, `migrations/`, `admin.py`, `profiles.py`, `selection.py`, `eligibility.py`, `supervisor.py`, `ingest.py`, `coverage.py`, `forms.py`, `views.py`, `urls.py`, `templates/passive/*.html`, `aprs/__init__.py`, `aprs/ax25.py`, `aprs/parse.py`, `aprs/igate.py`, `aprs/worker.py`, `management/commands/aprs_igate.py`.
- `apps/accounts/permissions.py`, `docs/permissions.md` (modify).
- `apps/monitoring/models.py`, `engine.py` + migrations (modify).
- `apps/stations/models.py` (EventType), `forms.py`/`views.py`/templates (modify).
- `templates/includes/sidebar.html`, `apps/stations/templates/stations/station_detail.html` (modify).
- `static/vendor/leaflet/`, `static/js/aprs-map.js`, `static/js/aprs-logic.js` (create); `static/js/control-panel.js`, `static/js/audio-panel.js` (modify).
- `docker-compose.yml`, `deploy/docker-compose.prod.yml`, `config/settings/base.py`, `CLAUDE.md` (modify).

---

### Task 1: Lock hook pair + regression-first refactor of every lock path

**Files:**
- Create: `apps/control/lock_hooks.py`
- Modify: `apps/control/consumers.py`: `disconnect` 146-176, `_sweep_loop` 342-365, `_lock_acquire` 1283-1291, `_revoke_if_holder` 1293-1308, `_lock_release` 1310-1320, `_lock_transfer` 1337-1351, `_lock_preempt` 1353-1377
- Test: `tests/test_control_lock_hooks.py`

**Interfaces:**
- Produces:
  - `async def on_lock_released(station, reason: str, *, stop_scan: bool = True, clear_tx_route: bool = False) -> None`
  - `async def on_lock_acquired(station, holder, reason: str, *, previous_holder_lost: bool) -> None`
  - `def register_release_hook(fn: Callable[[station, str], Awaitable[None]]) -> None`
  - `def register_acquire_hook(fn: Callable[[station, holder, str], Awaitable[None]]) -> None`
  - `reason` values: `released`, `revoked`, `expired`, `agent_disconnect`, `acquired`, `transferred`, `preempted`.
  - Registered hooks run **after** the built-in sequence. Each one runs in its own `try/except Exception` with `logger.exception`, so a hook failure never breaks a lock path.

The built-in sequence must equal today's behaviour exactly. Release: `stop_active_scan(reason_text)` if `stop_scan`, `gate.clear_ptt`, `gate.clear_tx_route` if `clear_tx_route`, then the `audio.gate` frame to browser and agent groups (body of `_bridge_gate`, consumers.py:1616). Acquire with `previous_holder_lost=True`: `stop_active_scan(reason_text, unless_user=holder if reason=="preempted" else None)`, `clear_ptt`, gate frame. Acquire with `previous_holder_lost=False` runs no built-in steps. Audit writes and `_broadcast_lock` stay at the call sites (their messages are user-specific). Keep the existing scan-stop reason strings ("lock released", "lock expired", "lock transferred", "lock preempted").

- [ ] **Step 1: Write the regression tests against today's code (they must PASS before the refactor)**

One test per path. Each records the channel-layer traffic to `control_<id>` and `control_<id>_agent` (spy pattern from `tests/test_control_consumer_lock.py`) plus the `ActiveScan`/gate state, and asserts the exact frame sequence:

```python
def test_release_path_sequence(...):        # scan_stop to agent, audio.gate x2, control.lock
def test_revoke_path_clears_tx_route(...):  # + gate.clear_tx_route effect
def test_sweep_expiry_sequence(...):        # "lock expired" scan stop, gate frame, lock broadcast
def test_agent_disconnect_sequence(...):    # no scan_stop frame (agent gone), ptt cleared, agent_offline then lock
def test_acquire_sequence(...):             # only audit + lock broadcast, no gate frame
def test_transfer_sequence(...):            # "lock transferred" stop, gate frame
def test_preempt_keeps_preemptors_scan(...):# unless_user honoured
```

- [ ] **Step 2: Run them on unmodified code**

Run: `python -m pytest tests/test_control_lock_hooks.py -q`
Expected: all PASS (this baseline is the regression net).

- [ ] **Step 3: Add the hook tests**

```python
def test_registered_release_hook_called_with_reason_for_every_release_path(...):
    # released, revoked, expired, agent_disconnect -> hook(station, reason)
def test_registered_acquire_hook_called_for_acquire_transfer_preempt(...):
    # acquired / transferred / preempted -> hook(station, holder, reason)
def test_failing_hook_does_not_break_lock_path(...):
    # hook raises -> lock broadcast still sent
```

Run: `python -m pytest tests/test_control_lock_hooks.py -q`
Expected: the new tests FAIL (`ImportError: lock_hooks`).

- [ ] **Step 4: Implement `apps/control/lock_hooks.py` and route all seven paths through it**

Remove the duplicated agent/browser `_bridge_gate` / `_do_clear_ptt` / `_audio_clear_ptt` call chains at the call sites, but keep the methods if other code still uses them (`_apply_scan_state` uses `_bridge_gate`). The request→grant path is the transfer, so `_lock_request` needs no hook call. Use `gettext`-free plain reason strings.

- [ ] **Step 5: Run the full control test suite**

Run: `python -m pytest tests/test_control_lock_hooks.py tests/test_control_consumer_lock.py tests/test_control_consumer_drain.py tests/test_control_consumer_relay.py tests/test_control_lock.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control/lock_hooks.py apps/control/consumers.py tests/test_control_lock_hooks.py
git commit -m "control: single on_lock_released/on_lock_acquired hook pair for every lock path"
```

---

### Task 2: Agent protocol frames + KISS de-framer

**Files:**
- Modify: `station_agent/protocol.py` (builders after `build_inventory`, :77)
- Create: `station_agent/decoders/__init__.py`, `station_agent/decoders/kiss.py`
- Modify: `station_agent/pyproject.toml` (`packages` += `"station_agent.decoders"`)
- Test: `tests/test_protocol.py` (extend), `tests/test_decoder_kiss.py`

**Interfaces:**
- Produces (protocol.py):
  - `build_inventory(slots: list, features: list[str] | None = None) -> dict` (adds `"features"` only when given)
  - `build_decoder_state(decoder: str, slot: int, state: str, detail: str | None = None) -> dict`
  - `build_decoder_snapshot(decoders: list[dict]) -> dict`
  - `build_decoder_output(decoder: str, slot: int, epoch: str, seq: int, kind: str, reliable: bool, data: dict, ts: float) -> dict`
  - `DECODER_STATES = ("starting", "running", "stopped", "error")`
- Produces (`decoders/__init__.py`): `FEATURE = "decoders"`.
- Produces (kiss.py): `class KissDeframer` with `feed(data: bytes) -> list[bytes]`. It returns the AX.25 payloads of data frames on port 0 (command byte `0x00`). It un-escapes `FESC TFEND` → `0xC0` and `FESC TFESC` → `0xDB`, drops non-data commands, drops frames longer than `MAX_FRAME = 1024` bytes, and keeps a partial frame across calls.

- [ ] **Step 1: Write the failing tests**

```python
def test_inventory_features_only_when_given():
    assert "features" not in build_inventory([])
    assert build_inventory([], features=["decoders"])["features"] == ["decoders"]

def test_decoder_output_envelope():
    f = build_decoder_output("aprs", 1, "e1", 7, "ax25", True, {"ax25": "AA=="}, 12.5)
    assert f == {"v": 1, "type": "decoder_output", "decoder": "aprs", "slot": 1, "epoch": "e1",
                 "seq": 7, "kind": "ax25", "reliable": True, "data": {"ax25": "AA=="}, "ts": 12.5}

def test_kiss_unescapes_and_splits():
    d = KissDeframer()
    assert d.feed(b"\xc0\x00ab\xdb\xdccd\xc0\xc0\x00x") == [b"ab\xc0cd"]
    assert d.feed(b"y\xdb\xdd\xc0") == [b"xy\xdb"]

def test_kiss_ignores_non_data_and_port1():
    assert KissDeframer().feed(b"\xc0\x01zz\xc0\xc0\x10zz\xc0") == []

def test_kiss_drops_oversized():
    assert KissDeframer().feed(b"\xc0\x00" + b"a" * 1025 + b"\xc0") == []
```

- [ ] **Step 2:** Run `python -m pytest tests/test_decoder_kiss.py tests/test_protocol.py -q`. Expected: FAIL (import errors).
- [ ] **Step 3:** Implement the builders and `KissDeframer`. Add the package to `pyproject.toml`.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "station-agent: decoder frames + KISS deframer"`

---

### Task 3: Reliable-output spool

**Files:**
- Create: `station_agent/decoders/spool.py`
- Test: `tests/test_decoder_spool.py`

**Interfaces:**
- Produces:
  - `class Spool(max_items: int = 2000, max_bytes: int = 1_000_000, *, epoch: str | None = None)`
  - `.epoch: str`: `uuid.uuid4().hex` when not given; fixed for the process lifetime
  - `.add(decoder: str, slot: int, kind: str, data: dict, ts: float) -> dict`: assigns `seq` (1, 2, …), stores and returns the full `decoder_output` frame (`reliable=True`)
  - `.ack(epoch: str, seqs: Iterable[int]) -> int`: removes the matching entries and returns how many were removed. A foreign epoch is a no-op.
  - `.pending() -> list[dict]`: in seq order
  - `.dropped: int`
  - Size accounting uses `len(json.dumps(frame))`. When `max_items` or `max_bytes` is exceeded, the **oldest** entries are dropped and counted.

- [ ] **Step 1: Write the failing tests**

```python
def test_seq_monotonic_and_pending_order(): ...           # seqs 1,2,3; pending() in order
def test_ack_removes_only_matching_epoch():
    s = Spool(epoch="A"); s.add("aprs", 1, "ax25", {}, 1.0); s.add("aprs", 1, "ax25", {}, 1.0)
    assert s.ack("B", [1]) == 0 and len(s.pending()) == 2
    assert s.ack("A", [1, 99]) == 1 and [f["seq"] for f in s.pending()] == [2]
def test_bound_by_items_drops_oldest():
    s = Spool(max_items=3); [s.add("aprs", 1, "ax25", {"i": i}, 0.0) for i in range(5)]
    assert [f["seq"] for f in s.pending()] == [3, 4, 5] and s.dropped == 2
def test_bound_by_bytes_drops_oldest(): ...               # max_bytes small, big data
def test_seq_continues_after_ack(): ...                   # no reuse of seq numbers
```

- [ ] **Step 2:** Run `python -m pytest tests/test_decoder_spool.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement with a `collections.OrderedDict[int, (frame, size)]`.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "station-agent: bounded decoder output spool with epoch/seq/ack"`

---

### Task 4: systemd template-unit control

**Files:**
- Create: `station_agent/decoders/units.py`
- Test: `tests/test_decoder_units.py`

**Interfaces:**
- Produces:
  - `UNIT_TEMPLATE = "oe5xrx-decoder@{instance}.service"`
  - `ENV_DIR = "/run/oe5xrx/decoder"`
  - `class DecoderUnitError(Exception)`
  - `class DecoderUnits(*, run=subprocess.run, env_dir: str = ENV_DIR, timeout_s: float = 15.0)`
  - `.instance(decoder: str, slot: int) -> str` → `"aprs-slot1"`
  - `.start(decoder: str, slot: int, env: dict[str, str]) -> None`: writes `<env_dir>/<instance>.env` atomically (tmp + `os.replace`, mode 0644, dir created 0755), then `systemctl start <unit>`
  - `.stop(decoder: str, slot: int) -> None`: `systemctl stop <unit>`, then removes the env file
  - `.state(decoder: str, slot: int) -> str`: result of `systemctl is-active <unit>`: `"active"|"activating"|"failed"|"inactive"|...` (stdout stripped; `"unknown"` on error)
  - `.stop_all() -> None`: `systemctl stop "oe5xrx-decoder@*.service"` and remove every `*.env` in `env_dir`
- All calls are blocking. Callers use an executor. Pattern: `station_agent/module_ota_sim.py:246-256` (injected `run`, `check=False`, `capture_output=True`, `text=True`, catches `(subprocess.TimeoutExpired, OSError)`). A non-zero return code on start/stop raises `DecoderUnitError(stderr.strip()[:200])`.
- Env keys must match `^OE5XRX_[A-Z_]+$` and values `^[A-Za-z0-9._:@/+-]{1,200}$`; anything else raises `DecoderUnitError("bad env")` **before** any file is written. Decoder names must match `^[a-z][a-z0-9]{0,15}$`.

- [ ] **Step 1: Write the failing tests** (fake `run` records argv and returns a `CompletedProcess`; `env_dir=tmp_path`)

```python
def test_start_writes_env_then_starts_unit(tmp_path): ...  # env content "OE5XRX_SLOT=1\n..."; argv == ["systemctl","start","oe5xrx-decoder@aprs-slot1.service"]
def test_start_rejects_newline_in_env_value(tmp_path): ... # no file, no systemctl call
def test_start_nonzero_raises(tmp_path): ...
def test_stop_removes_env(tmp_path): ...
def test_state_timeout_returns_unknown(tmp_path): ...
def test_stop_all_globs_and_cleans(tmp_path): ...
def test_bad_decoder_name_rejected(tmp_path): ...           # "../x", "APRS"
```

- [ ] **Step 2:** Run `python -m pytest tests/test_decoder_units.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "station-agent: systemd template unit control for decoders"`

---

### Task 5: `DecoderHost` + APRS plugin

**Files:**
- Create: `station_agent/decoders/aprs.py`, `station_agent/decoders/host.py`
- Test: `tests/test_decoder_host.py`, `tests/test_decoder_aprs.py`

**Interfaces:**
- Consumes: `KissDeframer` (Task 2), `Spool` (Task 3), `DecoderUnits` (Task 4), the `build_decoder_*` builders and `build_result` (`protocol.py:39`).
- Produces (aprs.py): `class AprsDecoder` with
  - `name = "aprs"`
  - `KISS_PORT_BASE = 8100`
  - `env(slot: int, params: dict, rx_node: str) -> dict[str, str]`: keys `OE5XRX_DECODER`, `OE5XRX_SLOT`, `OE5XRX_RX_NODE`, `OE5XRX_KISS_PORT` (= `8100 + slot`), `OE5XRX_SAMPLE_RATE` (`"48000"`)
  - `async def run(slot: int, emit: Callable[[str, bool, dict], Awaitable[None]], *, connect=asyncio.open_connection, now=time.time, sleep=asyncio.sleep) -> None`: connects to `127.0.0.1:<port>`. It retries every 0.5 s for up to 20 s; after that it raises `ConnectionError`. It reconnects the same way on EOF. For each KISS frame it calls `emit("ax25", True, {"ax25": base64(frame), "heard_at": now()})`.
- Produces (host.py):
  - `class DecoderHost(units: DecoderUnits, plugins: dict[str, plugin], resolve_rx_node: Callable[[int], str | None], *, spool: Spool | None = None, loop_executor=None, now=time.time, sleep=asyncio.sleep, health_poll_s: float = 5.0)`
  - `attach(send: Callable[[dict], Awaitable[None]]) -> None`
  - `detach() -> None`
  - `async start(decoder: str, slot: int, params: dict, request_id: str, max_runtime_s: float | None = None) -> None`
  - `async stop(decoder: str, slot: int, request_id: str | None = None, reason: str = "requested") -> None`
  - `async stop_slot(slot: int, reason: str) -> None`
  - `async reconcile_slots(present: set[int]) -> None`: stops instances whose slot is gone, reason `"module_gone"`
  - `async ack(epoch: str, seqs: list[int]) -> None`
  - `async on_connected() -> None`: sends `decoder_snapshot`, then every `spool.pending()` frame
  - `async startup() -> None`: `units.stop_all()` in the executor
  - `async shutdown() -> None`
  - `running() -> list[dict]`
- Behaviour:
  - `start` sends `result` ok/err for `request_id`, plus `decoder_state` `starting` → `running`, or `error` with `detail`.
  - Error codes:
    - `unknown_decoder` (plugin missing)
    - `no_rx_node` (resolver returns None)
    - `unit_failed` (`DecoderUnitError`)
    - `busy_slot` is **not** used. A different decoder on the same slot is stopped first, reason `"replaced"` (last start wins).
  - Starting the same decoder+slot again while it is running is idempotent: result ok, no restart.
  - The plugin reader task and a health task (polling `units.state` every `health_poll_s`) both run per instance. Health `failed`, or a reader `ConnectionError`, leads to `decoder_state error` and the unit is stopped. `activating` counts as running.
  - `max_runtime_s` triggers `stop(..., reason="max_runtime")`.
  - `stop` cancels the tasks, calls `units.stop` and sends `decoder_state stopped` with `detail=reason`.
  - Outputs with `reliable=True` go through `spool.add` and are sent if attached. Outputs with `reliable=False` are sent only if attached, otherwise dropped.
  - `send` failures are swallowed (pattern: `scanner.py:489-495`).
  - While detached, state frames are not queued. `on_connected` sends the snapshot.

- [ ] **Step 1: Write the failing tests** (fake units, fake plugin with a controllable `run`, `Collector` send, `asyncio.run`)

```python
def test_host_stops_leftover_units_on_startup(): ...            # startup() -> units.stop_all called once
def test_start_ok_sends_result_and_states():
    # types in order: decoder_state(starting), result(ok), decoder_state(running)
def test_start_unknown_decoder_error(): ...
def test_start_no_rx_node_error(): ...                         # no systemctl call
def test_start_same_twice_is_idempotent(): ...
def test_start_other_decoder_on_slot_replaces(): ...           # first stopped with detail "replaced"
def test_unit_failed_health_reports_error_and_stops(): ...
def test_max_runtime_stops(): ...                              # fake sleep, detail "max_runtime"
def test_reliable_output_spooled_while_detached_and_resent_on_connect():
    # detach, plugin emits 2 -> nothing sent; attach+on_connected -> snapshot then 2 outputs seq 1,2
def test_ack_prevents_resend(): ...
def test_unreliable_output_dropped_while_detached(): ...
def test_reconcile_stops_missing_slot(): ...                    # detail "module_gone"
def test_no_decoder_runs_without_start(): ...                  # host built + startup -> no units.start
```

`tests/test_decoder_aprs.py`:

```python
def test_env_contents():
    assert AprsDecoder().env(2, {}, "alsa_input.x") == {
        "OE5XRX_DECODER": "aprs", "OE5XRX_SLOT": "2", "OE5XRX_RX_NODE": "alsa_input.x",
        "OE5XRX_KISS_PORT": "8102", "OE5XRX_SAMPLE_RATE": "48000"}
def test_run_emits_ax25_frames_with_heard_at(): ...   # fake connect -> StreamReader fed KISS bytes
def test_run_gives_up_after_20s_without_listener(): ... # fake sleep/now -> ConnectionError
```

- [ ] **Step 2:** Run `python -m pytest tests/test_decoder_host.py tests/test_decoder_aprs.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Keep the per-instance state in one dataclass `_Instance(decoder, slot, reader_task, health_task, runtime_task, stopping)`.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "station-agent: generic decoder host + APRS (Direwolf KISS) plugin"`

---

### Task 6: Wire the host into Broker / ControlClient / agent

**Files:**
- Modify:
  - `station_agent/broker.py`: `__init__` :46-60 (new kwarg `decoder_host=None`), `handle` :169-194 (three `elif`s before the `else`)
  - `station_agent/control_client.py`: `__init__`; `_run_async` :468-489 (create the host once and `await host.startup()`); `_connect_and_serve` :195-276 (pass `decoder_host`; after `emit_inventory`: `host.attach(self._ws_send)`, `await host.on_connected()`, `await host.reconcile_slots(...)`; in `finally`: `host.detach()`); `_rediscovery_loop` :360-362 (`reconcile_slots` after emit); `emit_inventory` call adds `features=[decoders.FEATURE]`
  - `station_agent/broker.py` `emit_inventory` :470-523 (pass features through)
  - `station_agent/agent.py` :744-799 (resolver = `audio` backend `resolve_node(slot, "rx")`; `ControlClient(..., decoder_host_factory=...)`)
- Test: `tests/test_broker_decoders.py`, `tests/test_control_client.py` (extend)

**Interfaces:**
- Consumes: `DecoderHost` (Task 5).
- Produces:
  - `Broker(..., decoder_host: DecoderHost | None = None)`. Without a host, `decoder_start` answers `result(ok=False, error=("unsupported",""))` and `decoder_stop`/`decoder_ack` are ignored.
  - `ControlClient(config, ..., decoder_host_factory: Callable[[], DecoderHost] | None = None)`
  - The inventory frame now carries `"features": ["decoders"]` only when a host exists.
  - `Broker.on_disconnect` / `stop` do **not** stop decoders (see Decisions). `ControlClient` process shutdown calls `host.shutdown()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_broker_routes_decoder_frames_to_host(): ...         # start/stop/ack reach a fake host with the right args
def test_broker_without_host_rejects_start_unsupported(): ...
def test_broker_ignores_malformed_decoder_start(): ...      # missing slot/decoder/request_id -> result(validation_failed) if rid else ignored
def test_inventory_advertises_decoders_feature(): ...        # control_client against local ws server
def test_decoders_survive_ws_reconnect_and_resend(): ...      # host emits while disconnected; after reconnect server sees snapshot + spooled outputs
def test_rediscovery_module_gone_stops_slot_decoder(): ...
```

- [ ] **Step 2:** Run `python -m pytest tests/test_broker_decoders.py tests/test_control_client.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement the wiring.
- [ ] **Step 4:** Run `python -m pytest tests/test_broker*.py tests/test_control_client.py tests/test_scanner.py tests/test_agent_*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "station-agent: wire decoder host into control client (persistent across WS reconnects)"`

---

### Task 7: `apps.passive` models, admin, permission area

**Files:**
- Create: `apps/passive/{__init__,apps,models,admin}.py`, `apps/passive/migrations/0001_initial.py`
- Modify:
  - `config/settings/base.py` INSTALLED_APPS (after `"apps.repeaters"`, :55)
  - `apps/accounts/permissions.py` `AREAS` (:61-138)
  - `docs/permissions.md`
  - `tests/test_permissions_registry.py` `SPEC_MATRIX`
  - `apps/stations/models.py` `StationAuditLog.EventType` (+ `PASSIVE_SERVICE = "passive_service"`) + migration
- Test: `tests/test_passive_models.py`

**Interfaces:**
- Produces (models):
  - `PassiveServiceConfig(service: CharField unique choices [("aprs","APRS")], enabled: bool=False, idle_minutes: PositiveInt=10, profile: JSON)`, plus `@classmethod get(service) -> PassiveServiceConfig` (get_or_create with defaults).
    - Default `profile = {"frequency": 144.8, "squelch": 0, "rx_tone": "none", "tx_tone": "none", "bandwidth": "25"}`.
  - `HandoverProfile(values: JSON)` singleton with `@classmethod get()`. Default `{"frequency": 145.5, "squelch": 1, "rx_tone": "none", "tx_tone": "none"}`.
  - `AprsSettings` singleton `get()`, with fields:
    - `aprsis_host="rotate.aprs2.net"`, `aprsis_port=14580`, `aprsis_filter=""`
    - `max_gate_age_s=30`, `retention_days=30`, `no_rx_alert_hours=6`, `beacon_interval_min=30`
    - `beacon_comment="OE5XRX Remote Station RX-iGate"`
  - `StationPassiveConfig(station: OneToOne related_name="passive_config", enabled: bool=False, igate_callsign: CharField(6, blank), igate_ssid: PositiveSmallInt null)`.
    - Property `igate_call -> str | None` returns `"CALL-SSID"`, or `None` if either part is missing.
    - DB `UniqueConstraint(fields=["igate_callsign","igate_ssid"], condition=~Q(igate_callsign=""))`.
  - `PassiveServiceRun(station FK, service, slot: CharField(8), module_id, state choices starting|running|stopping|agent_offline|stopped|error, started_at, ended_at null, end_reason, last_error, last_packet_at null, packet_count: PositiveBigInt=0)`.
    - `UniqueConstraint(fields=["station"], condition=Q(ended_at__isnull=True), name="uniq_open_passive_run")`.
    - `@classmethod open_for(station_id) -> PassiveServiceRun | None`.
    - `RUNNING_STATES = {"running"}`.
  - `AprsPacket` fields:
    - `station FK`, `run FK SET_NULL null`
    - `epoch: CharField(32)`, `seq: PositiveBigInt`
    - `heard_at`, `received_at`
    - `raw_tnc2: TextField`, `source: CharField(9)`, `destination`, `path: JSON list`, `digipeated: bool`
    - `packet_type: CharField(16)`, `latitude`/`longitude` Decimal(9,6) null, `symbol: CharField(2)`, `comment: CharField(256)`
    - `gated_at null`, `gate_skip: CharField(24, blank)`
    - `UniqueConstraint(station, epoch, seq)`; indexes `(heard_at)`, `(station, heard_at)`, `(source, heard_at)`
  - `CoverageCell(station FK, cell_lat: Int, cell_lon: Int, count: PositiveBigInt, first_heard, last_heard, max_distance_km: Float)`, `UniqueConstraint(station, cell_lat, cell_lon)`.
- Produces (permissions): `"passive_services": Area(_("Passive Dienste"), view=A, manage=AD, verb_labels={"manage": _("Passive Dienste verwalten")})`.
- Produces (validation, `apps/passive/models.py`): `IGATE_CALL_RE = r"^(?=.{3,6}$)[A-Z0-9]{1,3}[0-9][A-Z]{1,4}$"` and `normalise_callsign(s: str) -> str` (strip + upper).

- [ ] **Step 1: Write the failing tests**

```python
def test_config_defaults(db):
    c = PassiveServiceConfig.get("aprs")
    assert (c.enabled, c.idle_minutes) == (False, 10)
    assert c.profile["frequency"] == 144.8 and c.profile["squelch"] == 0
    assert HandoverProfile.get().values["frequency"] == 145.5
    s = AprsSettings.get()
    assert (s.aprsis_host, s.aprsis_port, s.max_gate_age_s, s.retention_days,
            s.no_rx_alert_hours, s.beacon_interval_min) == ("rotate.aprs2.net", 14580, 30, 30, 6, 30)
def test_one_open_run_per_station(db, station): ...   # IntegrityError on 2nd open run
def test_igate_call_property(db, station): ...        # "OE5XRX-10"; None without ssid
def test_igate_call_unique_and_normalised(db, station_factory):
    # the form normalises " oe5xrx " -> "OE5XRX"; the DB constraint rejects a second (OE5XRX, 10)
def test_permission_area_passive_services():
    assert can(applicant, "passive_services", "view") and not can(staff, "passive_services", "manage")
```

The form part of `test_igate_call_unique_and_normalised` is completed in Task 16 (`StationPassiveForm`). Here only assert the DB constraint and `normalise_callsign`.

- [ ] **Step 2:** Run `python -m pytest tests/test_passive_models.py tests/test_permissions_registry.py tests/test_permissions_doc.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the models, migration, admin (read-only list for runs/packets), registry entry, and the doc regen (`python manage.py permissions_table > docs/permissions.md`, keeping the header that `test_permissions_doc` expects).
- [ ] **Step 4:** Run the same command plus `python manage.py makemigrations --check --dry-run`. Expected: PASS / "No changes detected".
- [ ] **Step 5: Commit** `git commit -m "passive: app, models, passive_services permission area"`

---

### Task 8: AX.25 → TNC2 + APRS info parser

**Files:**
- Create: `apps/passive/aprs/__init__.py`, `apps/passive/aprs/ax25.py`, `apps/passive/aprs/parse.py`
- Test: `tests/test_aprs_ax25.py`, `tests/test_aprs_parse.py`

**Interfaces:**
- Produces (ax25.py):
  - `class Ax25Error(ValueError)`
  - `@dataclass(frozen=True) Ax25Frame(source: str, destination: str, path: tuple[str, ...], info: bytes)`.
    - Path elements carry a trailing `*` on the **last** element whose H-bit is set (TNC2 convention).
  - `decode_ax25(frame: bytes) -> Ax25Frame`:
    - Addresses are 7-byte shifted. The extension bit ends the address field. At most 8 digipeaters.
    - Control must be `0x03` and PID `0xF0`, otherwise `Ax25Error`.
    - The callsign is stripped of spaces; SSID 0 is omitted.
  - `to_tnc2(f: Ax25Frame) -> str`: `"SRC>DST,P1,P2*:info"`, with info decoded as latin-1.
  - `is_digipeated(path: Sequence[str]) -> bool`: True if any element ends with `*`.
- Produces (parse.py):
  - `@dataclass AprsInfo(packet_type: str, latitude: float | None, longitude: float | None, symbol: str, comment: str)`.
  - `parse_info(info: bytes, destination: str) -> AprsInfo`. It never raises; a malformed packet gives `packet_type="other"` with no position.
  - Types:
    - `position` for `! = / @`, uncompressed and compressed
    - `mic_e` for `` ` `` and `'` (lat/lon from the destination)
    - `object` (`;`), `item` (`)`), `status` (`>`), `message` (`:`), `thirdparty` (`}`), `weather` (`_`), `other`
  - The comment is clipped to 256 characters and stripped of control chars.
  - `parse_thirdparty(info: bytes) -> tuple[str, str] | None`: returns `(inner_header "SRC>DST,PATH", inner_info)` for `}` packets.

- [ ] **Step 1: Write the failing tests** (build frames with a small `_ax25(src, dst, path, info, h_bits)` helper in the test)

```python
def test_decode_direct_frame():
    f = decode_ax25(_ax25("OE5ABC-9", "APRS", [], b"!4812.34N/01412.34E>hi"))
    assert to_tnc2(f) == "OE5ABC-9>APRS:!4812.34N/01412.34E>hi" and not is_digipeated(f.path)
def test_decode_digipeated_marks_last_used():
    f = decode_ax25(_ax25("OE5ABC", "APRS", ["OE5XBL-10", "WIDE2-1"], b">x", h_bits=[True, False]))
    assert f.path == ("OE5XBL-10*", "WIDE2-1") and is_digipeated(f.path)
def test_decode_rejects_non_ui_and_truncated(): ...   # pytest.raises(Ax25Error)
def test_uncompressed_position():
    i = parse_info(b"!4812.34N/01412.34E>comment", "APRS")
    assert i.packet_type == "position" and round(i.latitude, 4) == 48.2057 and round(i.longitude, 4) == 14.2057
    assert i.symbol == "/>" and i.comment == "comment"
def test_compressed_position(): ...   # "/5L!!<*e7>7P[" -> 49.5, -72.75 (APRS101 example)
def test_mic_e_position(): ...        # APRS101 example dest "S32U6T" -> 33 25.64 N
def test_southern_western_hemisphere_signs(): ...
def test_garbage_never_raises(): ...  # random bytes / truncated -> packet_type "other"
def test_thirdparty_split():
    assert parse_thirdparty(b"}OE1X>APRS,TCPIP,OE1Y*:>hi") == ("OE1X>APRS,TCPIP,OE1Y*", ">hi")
```

- [ ] **Step 2:** Run `python -m pytest tests/test_aprs_ax25.py tests/test_aprs_parse.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Use APRS101 chapters 8/9/10 for the formats (Mic-E: latitude digits and N/S/W/offset from the destination; longitude from info bytes 1–3 with the +100 / -180 rules).
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "passive: AX.25 decode + minimal APRS info parser"`

---

### Task 9: Profiles, slot selection, eligibility, `released_at`

**Files:**
- Create: `apps/passive/profiles.py`, `apps/passive/selection.py`, `apps/passive/eligibility.py`
- Modify:
  - `apps/control/models.py` `ControlLock` (+ `released_at = DateTimeField(null=True, blank=True)`)
  - `apps/control/lock.py` `_clear` :266 (set `released_at=timezone.now()`, add it to `update_fields`)
  - migration `apps/control/migrations/0012_controllock_released_at.py`
- Test: `tests/test_passive_profiles.py`, `tests/test_passive_eligibility.py`

**Interfaces:**
- Consumes: `apps.repeaters.selection._caps`/`module_band_of` (`apps/repeaters/selection.py:39,81`); `PersistedCapability` (`apps/control/models.py`); `lock.is_draining` (`apps/control/lock.py:61`).
- Produces (profiles.py):
  - `PROFILE_ORDER = ("frequency", "rx_frequency", "tx_frequency", "bandwidth", "rx_tone", "tx_tone", "squelch", "volume")`.
  - `profile_commands(values: dict, module: StationModule) -> list[tuple[str, object]]`. It keeps only caps that the module descriptor declares with `kind == "setting"`. It never keeps `filter_*` or anything outside `PROFILE_ORDER`, and it returns them in `PROFILE_ORDER`.
  - `handover_values(station, module: StationModule, handover: dict) -> dict`. It returns `handover` plus the station's persisted `volume` for that slot/module when `"volume"` is not in `handover`.
  - `profile_caps(values: dict, module) -> set[str]`: the cap names `profile_commands` would send.
- Produces (selection.py): `pick_slot(station, frequency_mhz: Decimal) -> StationModule | None`. It returns the online module with the lowest numeric slot whose `rx_frequency` ranges contain the frequency (`module_band_of(...) is not None`) and which is not `firmware_updating`.
- Produces (eligibility.py):
  - Reason codes: `REASONS = ("disabled", "not_opted_in", "offline", "agent_too_old", "updating", "draining", "lock_held", "idle_wait", "no_module")`.
  - `ineligible_reason(station, cfg: PassiveServiceConfig, *, now, agent_connected: bool, agent_features: set[str]) -> str | None`. Checks run in that order. `idle_wait` applies while `lock.released_at` is not NULL and `now - released_at < idle_minutes`. A `None` result means eligible.

- [ ] **Step 1: Write the failing tests**

```python
def test_profile_commands_filters_unknown_and_filter_caps(db, fm_module):
    cmds = profile_commands({"frequency": 144.8, "filter_hp": 1, "squelch": 0, "bogus": 1}, fm_module)
    assert cmds == [("frequency", 144.8), ("squelch", 0)]
def test_handover_adds_persisted_volume(db, station, fm_module): ...  # PersistedCapability volume=7 -> values["volume"] == 7
def test_handover_explicit_volume_wins(db, ...): ...
def test_pick_slot_first_matching_by_numeric_slot(db, station): ...   # slots "10" and "2" both 2m -> "2"
def test_pick_slot_none_for_70cm_only(db, station): ...
def test_released_at_set_by_every_free_path(db, station, member_user):
    # release, force_free, sweep_lock expiry -> released_at not None
@pytest.mark.parametrize("setup,reason", [...])   # one row per REASONS entry + eligible case
def test_ineligible_reason(db, station, setup, reason): ...
```

The `fm_module` fixture lives in this test file: a `StationModule` with a descriptor whose `rx_frequency.ranges=[{"name":"2m","min":144,"max":146}]`, settings `frequency`, `squelch`, `volume`, `rx_tone`, `tx_tone`, `bandwidth`, and `filter_hp`.

- [ ] **Step 2:** Run `python -m pytest tests/test_passive_profiles.py tests/test_passive_eligibility.py tests/test_control_lock.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement and add the migration.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "passive: profiles, slot selection, eligibility; ControlLock.released_at"`

---

### Task 10: `PassiveSupervisor`: scheduler, system commands, hand-over, reconcile

> Requires PR #196 merged and rebased in (the read-back makes system-issued `set`s show up as full `state` snapshots in every browser).

**Files:**
- Create: `apps/passive/supervisor.py`
- Modify:
  - `apps/control/consumers.py`:
    - `AgentControlConsumer.connect` :136-144: create `self.passive = PassiveSupervisor(...)`
    - `receive` :178-235: `inventory` stores `msg.get("features")`. New branches `decoder_state`, `decoder_snapshot` and `decoder_output` (the last one delegates to Task 11's `ingest`; until then it is a no-op ack-less stub). A `result` whose `request_id` starts with `"passive-"` goes to `self.passive.on_result`, **not** broadcast.
    - `_sweep_loop` :342-365: `await self.passive.tick()` after the reapply retry
    - `disconnect` :146-176: `await self.passive.on_agent_disconnect()`
    - new group handler `control_passive_preempt(event)`
    - `_reapply_persisted` :380 and `_retry_pending_reapply` :425: drop frames for `self.passive.protected_caps()`
    - `ControlConsumer.connect`: send the current passive snapshot
    - new browser handler `control_passive(event)` → `{"type":"passive_state", ...}`
  - `apps/passive/apps.py` `ready()`: `register_acquire_hook(preempt_hook)`
- Test: `tests/test_passive_supervisor.py`, `tests/test_passive_consumer.py`

**Interfaces:**
- Consumes:
  - `on_lock_acquired`/`register_acquire_hook` (Task 1)
  - `ineligible_reason`, `pick_slot`, `profile_commands`, `handover_values`, `profile_caps` (Task 9)
  - models (Task 7)
- Produces:
  - `class PassiveSupervisor(station_id: int, send_to_agent: Callable[[dict], Awaitable[None]], broadcast: Callable[[dict], Awaitable[None]], *, now=timezone.now, command_timeout: float = settings.CONTROL_COMMAND_TIMEOUT_SECONDS, sleep=asyncio.sleep)`, with these async methods:
    - `tick() -> None`
    - `on_result(msg: dict) -> None`
    - `on_decoder_state(msg: dict) -> None`
    - `on_snapshot(msg: dict) -> None`
    - `preempt(reason: str, *, handover: bool) -> None`
    - `on_agent_disconnect() -> None`
  - Sync methods: `set_agent_features(features: list | None) -> None` and `protected_caps() -> set[tuple[str, str, str]]`.
  - `async def preempt_hook(station, holder, reason) -> None` in `supervisor.py`. It group-sends `{"type": "control.passive_preempt", "reason": reason}` to `control_<id>_agent` **without awaiting any agent reply** (the lock grant stays immediate).
  - Browser frame: `{"type": "passive_state", "service": "aprs", "state": <run state or "off">, "slot": str|None, "reason": <ineligible reason or last_error or None>}`. The same payload is broadcast as group event `control.passive` on every run state change.
  - System command frame: `{"v":1,"type":"command","slot":…,"module":…,"capability":…,"op":"set","value":…,"request_id":"passive-<uuid4hex>"}`. It is sent sequentially; each one awaits its `result` future up to `command_timeout`.
- Sequences:
  - **tick (no open run):**
    - If `ineligible_reason(...)` is non-None, remember the reason for the badge and return.
    - Otherwise:
      1. Create the run (`state="starting"`).
      2. Send the profile commands one by one. Any failure or timeout → run `error`, back-off.
      3. **Re-check the lock is still free and no preempt was flagged.** If not, abort before `decoder_start`.
      4. Send `decoder_start{decoder:"aprs", slot, params:{}, request_id:"passive-…"}` and await its `result`.
      5. On `decoder_state running`, set the run to `running`.
    - Audit once per start: `PASSIVE_SERVICE`, "system:passive_services started aprs on slot N".
  - **tick (open run):**
    - If the station became `UPDATING` or draining, or the config was disabled or opted out, call `preempt(reason, handover=False)`.
    - If the lock is held (the acquire hook's group message was missed, e.g. a consumer restart), call `preempt("acquired", handover=True)`. This is the safety net behind the hook.
    - If an `agent_offline` run is older than 10 min with no reconnect, close it (`end_reason="agent_offline"`).
  - **preempt(reason, handover):**
    - Set the abort flag and cancel any in-flight start.
    - Send `decoder_stop` (best effort, timeout `command_timeout`, a failure is only logged).
    - If `handover`: send `handover_values(...)` as commands (sequential, failures logged).
    - Close the run (`state="stopped"`, `end_reason=reason`).
    - Audit, broadcast.
  - **on_agent_disconnect:** the run becomes `agent_offline` (not closed). Pending futures are cancelled.
  - **on_snapshot:**
    - If the snapshot lists `aprs` on the run's slot and the run is open and the station is eligible apart from `idle_wait`, set the run to `running`.
    - If it lists `aprs` with no open run, or the station is not eligible, send `decoder_stop`.
    - If the run is open but the snapshot is empty, close it with `end_reason="agent_restarted"`.
  - **Agent too old:** the inventory has no `"decoders"` in `features` → reason `agent_too_old`, no start attempted. A start whose result says `unsupported` sets `agent_too_old` until the next inventory.
  - **Back-off:** 60·2^n s capped at 900 s, kept in memory on the supervisor.

- [ ] **Step 1: Write the failing tests** (supervisor unit tests drive fake `send_to_agent` and `broadcast` and complete futures by calling `on_result`; consumer tests use the communicator)

```python
def test_tick_ineligible_does_nothing(...): ...
def test_tick_starts_profile_then_decoder(...):
    # commands frequency=144.8, bandwidth="25", rx_tone, tx_tone, squelch=0 in order, then decoder_start; run running
def test_profile_command_failure_backs_off(...): ...           # run error, next tick before 60 s sends nothing
def test_preempt_during_start_aborts_before_decoder_start(...):
    # complete first command, call preempt("acquired", handover=True), complete -> no decoder_start sent,
    # handover commands frequency=145.5, squelch=1 sent once, run end_reason "acquired"
def test_preempt_running_sends_stop_then_handover_with_persisted_volume(...): ...
def test_tick_preempts_when_lock_held_without_hook(...): ...   # safety net
def test_preempt_does_not_block_on_agent(...): ...   # preempt_hook returns without any agent reply (lock grant immediate)
def test_updating_or_draining_stops_without_handover(...): ...
def test_snapshot_empty_closes_open_run(...): ...
def test_snapshot_running_without_run_sends_stop(...): ...
def test_disconnect_marks_agent_offline_and_reconnect_resumes(...): ...
def test_agent_without_decoders_feature_is_too_old(...): ...
def test_passive_results_not_broadcast_to_browsers(...): ...   # consumer test
def test_reapply_skips_passive_profile_caps(...):
    # persisted volume=7, run open with profile volume=3 -> inventory reapply sends no volume set
def test_browser_gets_passive_state_on_connect_and_on_change(...): ...
def test_acquire_transfer_preempt_trigger_passive_preempt(...):  # via lock hook, all three paths
def test_system_writes_never_persisted(...): ...                 # PersistedCapability unchanged after profile apply
```

- [ ] **Step 2:** Run `python -m pytest tests/test_passive_supervisor.py tests/test_passive_consumer.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. DB access goes through `database_sync_to_async`. The `"passive-"` request-id namespace keeps the browser `pending` map untouched.
- [ ] **Step 4:** Run `python -m pytest tests/test_passive_*.py tests/test_control_*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "passive: supervisor (idle scheduler, system profile commands, hand-over, reconcile)"`

---

### Task 11: Packet ingest, ack, coverage aggregation

**Files:**
- Create: `apps/passive/ingest.py`, `apps/passive/coverage.py`
- Modify: `apps/control/consumers.py` (`decoder_output` branch → `ingest`, then send `decoder_ack`)
- Test: `tests/test_passive_ingest.py`

**Interfaces:**
- Consumes: `decode_ax25`, `to_tnc2`, `is_digipeated`, `parse_info` (Task 8); `AprsPacket`, `CoverageCell`, `PassiveServiceRun` (Task 7).
- Produces:
  - `MAX_SKEW = timedelta(days=7)`
  - `def server_heard_at(recv_now: datetime, ts: float, agent_heard_at: float) -> datetime`: `recv_now - clamp(ts - agent_heard_at, 0, MAX_SKEW)`
  - `def ingest_output(station_id: int, msg: dict, *, recv_now: datetime) -> bool`
    - Returns True when the frame should be acked: stored, duplicate, or undecodable. Returns False only for frames with a missing or invalid `epoch`/`seq`, which are never acked.
    - Only `kind == "ax25"` is stored. Other kinds return True and are ignored.
    - One transaction. Undecodable AX.25 → logged at most once per minute per station, acked, not stored.
    - Duplicate `(station, epoch, seq)` → acked, no second row.
    - Updates the open run: `last_packet_at`, `packet_count += 1`.
    - Calls `record_coverage` for direct packets with a position.
  - `cell_of(lat: float, lon: float) -> tuple[int, int]` (`floor(lat*50), floor(lon*50)`)
  - `record_coverage(station, packet: AprsPacket) -> None`
    - Only when `not packet.digipeated`, a position exists and `packet_type != "object"/"item"` (objects are not where the transmitter is).
    - Upserts `count`, `first_heard`, `last_heard`, and `max_distance_km` via `apps.repeaters.selection.haversine_km` (:124) from the station coordinates. When the station has no coordinates, distance is 0.
- Ack frame to the agent: `{"v":1,"type":"decoder_ack","epoch":…,"seqs":[seq]}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_heard_at_uses_agent_clock_delta_only():
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert server_heard_at(now, ts=5000.0, agent_heard_at=4990.0) == now - timedelta(seconds=10)
    assert server_heard_at(now, ts=10.0, agent_heard_at=50.0) == now           # negative -> 0
def test_ingest_stores_parsed_packet_and_updates_run(db, station, open_run): ...
def test_duplicate_seq_acked_not_stored_twice(db, ...): ...
def test_bad_ax25_acked_not_stored(db, ...): ...
def test_missing_seq_not_acked(db, ...): ...
def test_direct_position_updates_coverage(db, ...): ...    # count 1 -> 2 on same cell, max_distance grows
def test_digipeated_excluded_from_coverage(db, ...): ...
def test_object_excluded_from_coverage(db, ...): ...
def test_consumer_acks_after_ingest(...): ...               # communicator: decoder_output -> decoder_ack frame
```

- [ ] **Step 2:** Run `python -m pytest tests/test_passive_ingest.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "passive: APRS packet ingest, ack, direct-only coverage aggregates"`

---

### Task 12: APRS-IS gating rules, passcode, beacon (pure functions)

**Files:**
- Create: `apps/passive/aprs/igate.py`
- Test: `tests/test_aprs_igate.py`

**Interfaces:**
- Consumes: `AprsPacket` fields, `parse_thirdparty` (Task 8).
- Produces:
  - `NOGATE_TOKENS = ("TCPIP", "TCPXX", "NOGATE", "RFONLY")`
  - `TOCALL = "APZXRX"`
  - `def passcode(callsign: str) -> int`: the standard APRS-IS hash over the upper-case call **without** SSID
  - `def login_line(igate_call: str, filter_: str, version: str) -> str` → `"user CALL-SSID pass N vers OE5XRX-StationManager <version>[ filter <f>]\r\n"`
  - `def gate_line(packet: AprsPacket, igate_call: str, own_calls: set[str]) -> tuple[str | None, str]`. It returns `(line, "")` or `(None, skip_reason)`. Skip reasons:
    - `own_call`: the source is in `own_calls`
    - `nogate_path`: a path element, star-stripped, begins with one of the `NOGATE_TOKENS`
    - `query`: the info starts with `?`
    - `thirdparty_inet`: a third-party inner header contains `TCPIP`/`TCPXX`
    - `thirdparty_bad`: unparsable third-party
  - Third-party packets are unwrapped: the inner header and info are gated.
  - Line format: `"<src>>{dst}[,path...],qAR,<igate_call>:<info>\r\n"`, where the path is kept as heard, `*` included.
  - `def beacon_line(igate_call: str, lat: float, lon: float, comment: str) -> str` → `"<call>>APZXRX,TCPIP*:!DDMM.mmNR DDDMM.mmE&<comment>\r\n"`. The symbol table is `R` (overlay) and the code is `&`. Lat/lon are formatted `%02d%05.2f` / `%03d%05.2f`.

- [ ] **Step 1: Write the failing tests**

```python
def test_passcode_known_values():
    assert passcode("N0CALL") == 13023 and passcode("n0call-10") == 13023
def test_login_line():
    assert login_line("OE5XRX-10", "", "1.0") == "user OE5XRX-10 pass %d vers OE5XRX-StationManager 1.0\r\n" % passcode("OE5XRX")
def test_gate_direct(): ...            # "OE5ABC>APRS,WIDE1-1,qAR,OE5XRX-10:!...\r\n"
def test_gate_keeps_used_path_star(): ...
@pytest.mark.parametrize("path", [["TCPIP*"], ["WIDE1-1", "NOGATE"], ["RFONLY"], ["TCPXX*"]])
def test_gate_skips_nogate_paths(path): ...
def test_gate_skips_own_igate_call(): ...
def test_gate_thirdparty_unwrapped(): ...       # "}OE1X>APRS,WIDE1*:>hi" -> "OE1X>APRS,WIDE1*,qAR,OE5XRX-10:>hi\r\n"
def test_gate_thirdparty_from_inet_skipped(): ...
def test_gate_query_skipped(): ...
def test_beacon_line():
    assert beacon_line("OE5XRX-10", 48.2057, 14.2057, "OE5XRX Remote Station RX-iGate") == \
        "OE5XRX-10>APZXRX,TCPIP*:!4812.34NR01412.34E&OE5XRX Remote Station RX-iGate\r\n"
def test_beacon_southwest(): ...
```

- [ ] **Step 2:** Run `python -m pytest tests/test_aprs_igate.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "passive: APRS-IS gating rules, passcode, iGate beacon"`

---

### Task 13: `aprs_igate` worker + retention + compose services

**Files:**
- Create: `apps/passive/aprs/worker.py`, `apps/passive/management/__init__.py`, `apps/passive/management/commands/__init__.py`, `apps/passive/management/commands/aprs_igate.py`
- Modify: `docker-compose.yml` (after `repeater-sync`, :55-87), `deploy/docker-compose.prod.yml` (:50-76)
- Test: `tests/test_aprs_worker.py`

**Interfaces:**
- Consumes: `gate_line`, `login_line`, `beacon_line` (Task 12); models (Task 7).
- Produces:
  - `class IgateWorker(*, connect=asyncio.open_connection, now=timezone.now, sleep=asyncio.sleep, version: str = "1")`
  - `async step() -> None`: one iteration
  - `async run_forever(poll_s: float = 1.0) -> None`
  - `desired_logins() -> dict[str, IgateTarget]`
  - `async housekeeping(now) -> int`: returns deleted rows
  - `@dataclass IgateTarget(station_id: int, call: str, lat: float | None, lon: float | None)`
- `step()`:
  1. `desired_logins()`: open runs in state `running` (service `aprs`) whose station has `passive_config.igate_call` **and** coordinates, keyed by call (spec §6.4). Stations missing either are **not** logged in. Their packets get `gate_skip="no_igate_call"` or `"no_coordinates"`.
  2. Open missing connections with `login_line`, close connections no longer desired. A connect failure → per-call back-off of 30 s ×2 up to 600 s. Incoming server lines are read and discarded (a `# logresp … unverified` line is logged at WARNING once).
  3. Handle packets with `gated_at IS NULL AND gate_skip = ''`, ordered by id, at most 500:
     - `now - heard_at > max_gate_age_s` → `gate_skip="stale"`
     - station not in an open login → `gate_skip="not_running"`
     - else `gate_line(...)`: a line → write to the socket, then `gated_at=now`; `(None, reason)` → `gate_skip=reason`
     - a socket write error → close that connection and leave the packet as is (it goes `stale` later)
  4. Beacon every `beacon_interval_min` per connected call, when the station has coordinates.
  5. Every 24 h, `housekeeping`: `AprsPacket.objects.filter(heard_at__lt=now - retention_days)` deleted in batches of 5000.
- `own_calls` = all configured `igate_call`s.
- Command:
  - `aprs_igate [--once]` runs `asyncio.run(IgateWorker().run_forever())`.
  - `close_old_connections()` runs in every step (sync DB calls via `sync_to_async(thread_sensitive=True)`).
  - Exceptions inside `step` are logged and the loop continues (pattern: `apps/repeaters/management/commands/sync_repeaters.py`).
- Compose service (dev):

  ```yaml
    aprs-igate:
      build:
        context: .
        args:
          DEV: "true"
      command: python manage.py aprs_igate
      volumes:
        - .:/app
      env_file:
        - .env
      environment:
        - DJANGO_SETTINGS_MODULE=config.settings.dev
      depends_on:
        db:
          condition: service_healthy
      restart: unless-stopped
  ```

  Prod: the same as the `repeater-sync` block (`image: ghcr.io/oe5xrx/station-manager:latest`, prod settings, db+redis depends_on) with `command: python manage.py aprs_igate`.

- [ ] **Step 1: Write the failing tests** (fake `connect` returning an in-memory writer that records bytes; DB via `@pytest.mark.django_db(transaction=True)`)

```python
def test_logs_in_once_per_running_station_with_call(...): ...   # two stations -> two logins with their own CALL-SSID
def test_station_without_call_not_logged_in_packets_skipped(...): ...  # gate_skip "no_igate_call"
def test_station_without_coordinates_not_logged_in(...): ...         # gate_skip "no_coordinates", no beacon
def test_fresh_packet_gated_exactly_once(...): ...
def test_stale_packet_marked_not_gated(...): ...                # heard_at 31 s ago -> gate_skip "stale", nothing written
def test_nogate_path_marked(...): ...
def test_connection_closed_when_run_ends(...): ...
def test_connect_failure_backs_off(...): ...
def test_beacon_sent_on_login_and_after_interval(...): ...
def test_housekeeping_deletes_only_older_than_retention(...): ...  # coverage cells untouched
```

- [ ] **Step 2:** Run `python -m pytest tests/test_aprs_worker.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the worker, the command and both compose entries (there is no app version setting; the login uses the literal `vers OE5XRX-StationManager 1`).
- [ ] **Step 4:** Run `python -m pytest tests/test_aprs_worker.py -q && docker compose config -q`. Expected: PASS, no compose error.
- [ ] **Step 5: Commit** `git commit -m "passive: aprs_igate worker (per-station APRS-IS login, freshness gate, beacons, retention)"`

---

### Task 14: RX health alert

**Files:**
- Modify:
  - `apps/monitoring/models.py` `AlertRule.AlertType` (:35, + `PASSIVE_NO_RX = "passive_no_rx", _("Passive RX silent")`)
  - migrations: alter choices + a data migration seeding the rule like `0002_seed_default_rules`
  - `apps/monitoring/engine.py` (new `_check_passive_no_rx()` next to `_check_station_offline` :276, called from `check_alerts` :562)
  - notification text mapping if one exists (see `tests/test_notification_new_alert_types.py`)
- Test: `tests/test_passive_alert.py`

**Interfaces:**
- Consumes: `PassiveServiceRun`, `AprsSettings.no_rx_alert_hours`; `_get_active_rule`, `_has_unresolved_alert`, `_create_alert`, `_auto_resolve(alert_type, station)` (`engine.py:23,42,53,70`).
- Produces: `_check_passive_no_rx() -> list[Alert]`.
  - **Raise:** an open run in state `running` with `now - max(last_packet_at or started_at, started_at) >= no_rx_alert_hours` and no unresolved alert. Title: `"APRS RX still: <name>"`. Message: "no packets for N h — possible antenna/RX problem".
  - **Resolve:** stations with an unresolved alert whose open run has `last_packet_at` newer than the alert's `created_at`.
  - Runs that end without a packet do **not** resolve and do not raise (the run is closed). The alert stays until the next run hears something or someone acknowledges it.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_alert_for_run_shorter_than_threshold(db, ...): ...
def test_alert_raised_after_6h_without_packets(db, ...): ...
def test_alert_not_duplicated(db, ...): ...
def test_alert_resolved_on_next_packet(db, ...): ...
def test_threshold_from_settings(db, ...): ...   # no_rx_alert_hours=1
```

- [ ] **Step 2:** Run `python -m pytest tests/test_passive_alert.py tests/test_monitoring_check_alerts_wiring.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement and add the migrations.
- [ ] **Step 4:** Run the same command plus `python manage.py makemigrations --check --dry-run`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "monitoring: passive APRS no-RX alert"`

---

### Task 15: Central APRS page (map & log, iGates, settings)

> **Mandatory:** invoke `Skill("frontend-design")` before writing any template/CSS/JS.

**Files:**
- Create:
  - `apps/passive/{forms,views,urls}.py`
  - `apps/passive/templates/passive/aprs.html`
  - `static/vendor/leaflet/` (leaflet.js, leaflet.css, images/, LICENSE; version 1.9.4)
  - `static/js/aprs-logic.js` (pure: grouping and formatting), `static/js/aprs-map.js` (DOM/Leaflet)
- Modify:
  - `config/urls.py` (next to `repeaters/` :59 → `path("passive/", include("apps.passive.urls"))`)
  - `templates/includes/sidebar.html` :171-184
  - `config/settings/base.py` `SECURE_CSP` `img-src` (+ `"https://tile.openstreetmap.org"`)
  - `tests/test_permission_matrix.py` `UI` dict
- Test: `tests/test_passive_views.py`, `tests/js/aprs-logic.test.mjs`

**Interfaces:**
- Consumes: models (Task 7); `AreaPermissionMixin` (`apps/accounts/permissions.py:373`).
- Produces (URLs, `app_name = "passive"`):
  - `aprs` → `/passive/aprs/`: tabs `karte`, `igates`, `einstellungen`. View `passive_services.view`; the settings tab and POST need `manage`.
  - `aprs_packets` → `/passive/aprs/api/packets/?after_id=&station=&hours=24`. Returns JSON `{"packets":[{id, station_id, station_name, heard_at, source, packet_type, latitude, longitude, symbol, comment, raw_tnc2, gated}], "last_id"}`. At most 500, newest first. View `passive_services.view`.
  - `aprs_igates` → `/passive/aprs/api/igates/`: `[{station_id, name, call, state, reason, warning, last_packet_at, packets_24h}]`, where `state` ∈ `running|idle|in_use|error|not_opted_in|agent_offline` and `warning` ∈ `null|"no_igate_call"|"no_coordinates"`. The iGates tab shows the warning ("wird nicht ins APRS-IS gegated").
  - `aprs_settings` (POST) → `/passive/aprs/einstellungen/`. View `manage`. Forms: `PassiveServiceConfigForm` (enabled, idle_minutes ≥ 1, profile fields: frequency MHz, squelch 0–8, bandwidth choice 12.5/25, tones, volume; blank = omit), `HandoverProfileForm` (same profile fields), `AprsSettingsForm` (all `AprsSettings` fields; `max_gate_age_s` 1–300, `retention_days` ≥ 1, `no_rx_alert_hours` ≥ 1, `beacon_interval_min` 10–120).
- Produces (JS, `aprs-logic.js`, UMD like `static/js/metrics-history-logic.js`):
  - `groupDuplicates(packets) -> [{key, packet, stations:[names]}]`: the key is `raw_tnc2` minus the path, within a 30 s window
  - `latestPositions(packets) -> Map<source, packet>`
  - `formatAge(seconds) -> string`
- The map shows the latest position per source (last 24 h by default). The log is newest first with a station filter `<select>` and grouped duplicates ("gehört von -211, -205"). Polling every 5 s with `after_id`, paused while the tab is hidden (pattern: `tests/js/app-poll-visibility.test.mjs`).

- [ ] **Step 1: Invoke `Skill("frontend-design")`.**
- [ ] **Step 2: Write the failing tests**

```python
def test_applicant_sees_page_without_settings_tab(client, applicant_user): ...
def test_admin_sees_settings_tab_and_can_save(client, admin_user): ...  # numeric inputs carry lang="en"
def test_staff_cannot_post_settings(client, staff_user): ...           # 403
def test_packets_api_after_id_and_station_filter(client, applicant_user, ...): ...
def test_igates_api_states(client, ...): ...                            # each state value reachable
def test_settings_profile_blank_fields_omitted(client, admin_user): ... # frequency only -> profile == {"frequency": 144.8}
def test_template_no_multiline_hash_comments(): ...                     # existing template guard passes
```

`tests/js/aprs-logic.test.mjs`: `groupDuplicates` merges the same packet from two stations within 30 s and keeps them apart after 31 s; `latestPositions` keeps the newest per source.

- [ ] **Step 3:** Run `python -m pytest tests/test_passive_views.py tests/test_permission_guards.py tests/test_permission_matrix.py -q && node --test tests/js/aprs-logic.test.mjs`. Expected: FAIL.
- [ ] **Step 4:** Implement. Sidebar: inside the Radio section, add a sub-label "Passive Dienste" and the link "APRS" gated on `{% if can.passive_services.view %}`. Leaflet is loaded with `nonce="{{ csp_nonce }}"` like uPlot. Tile attribution: "© OpenStreetMap contributors".
- [ ] **Step 5:** Run the same command. Expected: PASS.
- [ ] **Step 6: Commit** `git commit -m "passive: APRS page (map, live log, iGates, admin settings)"`

---

### Task 16: Station APRS tab, opt-in form, status badge

> **Mandatory:** `Skill("frontend-design")`.

**Files:**
- Modify:
  - `apps/stations/templates/stations/station_detail.html` (tab button in :99-114, panel next to `telemetry` :366)
  - `apps/stations/views.py` `StationDetailView` :177 and `StationUpdateView` :329
  - `apps/stations/templates/stations/station_form.html`
- Create: `apps/passive/forms.py` `StationPassiveForm`, `apps/passive/templates/passive/_badge.html`, `apps/passive/templates/passive/_station_aprs_tab.html`
- Test: `tests/test_passive_station_ui.py`

**Interfaces:**
- Consumes: `aprs-map.js` (Task 15) with a `data-station-id` attribute to pre-filter; `/passive/aprs/api/packets/?station=`; `eligibility.ineligible_reason` (Task 9).
- Produces:
  - `StationPassiveForm(ModelForm of StationPassiveConfig, prefix="passive")` with fields `enabled`, `igate_callsign`, `igate_ssid`. `igate_ssid` initial is 10 and only a hint (placeholder). `clean()` behaves as follows:
    - When `enabled`, `igate_callsign` and `igate_ssid` are required.
    - The call is normalised with `normalise_callsign` and must match `IGATE_CALL_RE`.
    - SSID must be 0–15.
    - The `(call, ssid)` pair must be unique across stations, raising a form error, not an IntegrityError.
  - `StationUpdateView` renders and saves `StationPassiveForm` only when `request.user.can("passive_services", "manage")`. Otherwise it neither renders nor processes it, so a forged POST is ignored.
  - `passive_badge(station) -> dict` (in `apps/passive/views.py`), shared by the detail and control pages. It returns `{"label": "Passiv: APRS aktiv"|"Passiv: in Benutzung"|"Passiv: aus"|"Passiv: Fehler: <detail>", "level": "ok"|"muted"|"off"|"error"}`.
  - Tab "APRS" is shown when `station.passive_config.enabled`, gated on `can.passive_services.view`.

- [ ] **Step 1: Invoke `Skill("frontend-design")`.**
- [ ] **Step 2: Write the failing tests**

```python
def test_igate_call_unique_and_normalised(db, station_factory):   # completes the Task 7 test
    # form with " oe5xrx " + 10 -> saved "OE5XRX"; second station same call/ssid -> form error
def test_enabled_requires_call_and_ssid(db, ...): ...
def test_invalid_callsign_rejected(db, ...): ...     # "OE5", "1234", "OE5XRX-1"
def test_staff_post_cannot_change_passive_fields(client, staff_user, ...): ...
def test_detail_shows_aprs_tab_only_when_opted_in(client, applicant_user, ...): ...
@pytest.mark.parametrize("run_state,label", [("running","Passiv: APRS aktiv"), (None,"Passiv: aus"), ("error","Passiv: Fehler:")])
def test_badge_labels(db, ..., run_state, label): ...
def test_badge_in_use_when_lock_held(db, ...): ...
```

- [ ] **Step 3:** Run `python -m pytest tests/test_passive_station_ui.py tests/test_passive_models.py tests/test_stations_permissions.py -q`. Expected: FAIL.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** Run the same command. Expected: PASS.
- [ ] **Step 6: Commit** `git commit -m "passive: station APRS tab, opt-in + iGate call form, status badge"`

---

### Task 17: Control page passive badge + browser RX mixer starts muted

> **Mandatory:** `Skill("frontend-design")`.

**Files:**
- Modify:
  - `static/js/control-panel.js`: the message switch at :333-370 gets `case "passive_state"`, plus a new `_onPassive(msg)`
  - `static/js/audio-panel.js`: mixer at :57 and `toggleMute` :773; new `setPassiveMute(active: bool)`
  - `static/js/control-logic.js` (pure helper)
  - `apps/control/templates/control/panel.html` (badge slot + notice)
- Test: `tests/js/control-logic.test.mjs` (extend), `tests/js/control-panel-passive.test.mjs` (pattern: `tests/js/control-panel-readonly.test.mjs`)

**Interfaces:**
- Consumes: browser frame `{"type":"passive_state","service","state","slot","reason"}` (Task 10).
- Produces:
  - `ControlLogic.passiveMuteDecision(prev: {active, userUnmuted}, frame) -> {active: bool, muteNow: bool, unmuteNow: bool}`:
    - On the transition into `state == "running"`: `muteNow` is true and `userUnmuted` resets to false.
    - While running, after the user unmuted: no re-mute.
    - On the transition out of running: `unmuteNow` is true only if the mute was passive-imposed and the user did not touch it.
  - `AudioPanel.setPassiveMute(active)` mutes or unmutes every RX stream in `this.mixer` (gain path `A.effectiveGain`). A manual `toggleMute` during passive sets `userUnmuted`.
  - Notice text while muted: "Passiv: APRS läuft, Audio stumm". Clicking the unmute button lets the user listen in.
  - The badge uses the same labels as Task 16.
- Purely client-side. The Opus stream and the server are unchanged.

- [ ] **Step 1: Invoke `Skill("frontend-design")`.**
- [ ] **Step 2: Write the failing tests**

```js
test("entering passive mutes", () => { const d = passiveMuteDecision({active:false,userUnmuted:false}, {state:"running"}); assert.equal(d.muteNow, true); });
test("user unmute is respected while passive", ...);
test("leaving passive restores only passive-imposed mute", ...);
test("panel shows notice and badge on passive_state frame", ...);   // control-panel-passive.test.mjs
```

- [ ] **Step 3:** Run `node --test tests/js/`. Expected: the new tests FAIL.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** Run `node --test tests/js/`. Expected: PASS.
- [ ] **Step 6: Commit** `git commit -m "control: passive badge + RX mixer starts muted during passive APRS"`

---

### Task 18: E2E (probe) + docs

**Files:**
- Create: `tests/test_passive_e2e.py`
- Modify: `CLAUDE.md` (new section "Passive Dienste / APRS (`apps/passive`, `station_agent/decoders`)", 8–12 lines, invariants only)

**Interfaces:**
- Consumes: everything above. Uses the real `Broker` + `DecoderHost`, with a fake `DecoderUnits` and a fake APRS plugin that emits given KISS frames. It talks to the real `AgentControlConsumer` through `WebsocketCommunicator` and to a real `IgateWorker` with a fake `connect`.

- [ ] **Step 1: Write the E2E test**

```python
def test_idle_station_runs_aprs_end_to_end(...):
    # 1. station opted in (OE5XRX-10, coordinates), config enabled, idle_minutes=1, lock released_at 2 min ago
    # 2. agent connects with features ["decoders"] and a 2 m FM module
    # 3. supervisor tick -> profile commands (agent answers ok) -> decoder_start -> decoder_state running
    # 4. agent emits 3 frames: direct position (fresh), digipeated position (fresh), direct position heard 40 s ago
    # 5. server acks all 3 seqs; 3 AprsPacket rows; 1 coverage cell (only the fresh direct one + the old direct one -> count 2)
    # 6. IgateWorker.step(): fake APRS-IS receives login "user OE5XRX-10 pass ..." then exactly 2 lines
    #    (direct fresh + digipeated fresh, both with ",qAR,OE5XRX-10:"), the 40 s old one has gate_skip "stale"
    # 7. a member acquires the lock -> agent receives decoder_stop then set frequency=145.5, squelch=1
def test_ws_drop_resend_dedup(...):
    # agent spools 2 outputs during a disconnect, reconnects, resends; one was already stored -> 2 rows total, both acked
```

- [ ] **Step 2:** Run `python -m pytest tests/test_passive_e2e.py -q`. Expected: PASS (if it fails, fix the owning task's code, not the test's intent).
- [ ] **Step 3:** Run the full suite and lint: `python -m pytest -q && node --test tests/js/ && ruff check . && ruff format --check .`. Expected: all green.
- [ ] **Step 4:** Add the CLAUDE.md section. Cover: lock-hook pair = only place for lock-side effects; decoders survive WS drops, agent start stops leftovers; `passive-` request ids; system writes never persisted; drift re-apply skips protected caps; freshness on server clock; worker = only APRS-IS speaker; servers repo needs `aprs-igate`.
- [ ] **Step 5: Commit** `git commit -m "passive: E2E test + CLAUDE.md invariants"`

---

## After the last task

- Open the single PR against `main` and run the review-loop (project rule). Mention in the PR body: deploy order (server first, dormant until enabled → image with Direwolf + decoder unit + agent bump → per-station opt-in), and the companion image plan.
- HIL: the .211 spike from spec §10 (second PipeWire consumer, decode rate) is done during the image plan, Task 3, before any station is opted in.
