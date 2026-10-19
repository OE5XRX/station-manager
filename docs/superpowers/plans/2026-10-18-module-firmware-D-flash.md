# Teilbereich D — Modul-Firmware flashen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The station agent actually executes C's reconcile instructions (download → flash via `dfu-util` or sim swap → verify → status/commit). The server adds drain, the idle gate, a nudge, the sim artifact and the UI states.

**Architecture:** Server: a new `apps/module_firmware/drain.py` holds one predicate, "convergence possible + pending". It gates both `ReconcileCheckView` (with the idle gate: no ControlLock held) and `apps/control/lock.py` (no new lock grants). Agent: a new `station_agent/module_ota.py` runs from the main heartbeat thread with pluggable backends (`DfuBackend`, `SimBackend`), a thread-safe `slot_pause` registry honoured by inventory, debouncer, rediscovery and broker, and a crash marker in `state_dir`.

**Tech Stack:** Django 6.0, DRF, Channels; Python 3.14 agent (stdlib + requests + pyserial); pytest; Alpine.js control panel.

**Spec:** `docs/superpowers/specs/2026-10-18-module-firmware-D-flash-design.md` (where they conflict, „Klassifikation & Fehlerfälle" wins). The sim flash follows linux-image `docs/module-firmware-flash.md` **verbatim**; the steps are copied into Task A5.

## Global Constraints

- Tests are top-level `tests/test_*.py` (server module-firmware tests may go in `tests/module_firmware/`). Run with `python -m pytest -q`; lint with `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .`.
- Django templates never use multi-line `{# #}`; use `{% comment %}`.
- Never touch `apps/api/` except `apps/api/serializers.py` + `apps/api/views.py` for the heartbeat field (Task S1). Another child owns the rest of `apps/api`.
- Server→agent frames: `{"v": 1, "type": "firmware_check"}` via `group_send(f"control_{station_id}_agent", {"type": "control.to_agent", "frame": frame})`.
- Agent→server events (existing `event` frame, `protocol.build_event`): `firmware_update_started` (detail `{"target_version", "convergence_id"}`), `firmware_update_finished` (detail `{"result": "success"|"rolled_back"|"rejected"|"failed", "target_version"}`), `firmware_dfu_stuck` (detail `{"msg": "Modul hängt im DFU-Modus — Power-Cycle nötig"}`).
- Inventory module entry of a paused slot: last-known entry plus `"status": "updating"` (heartbeat inventory and control inventory).
- Heartbeat payload key: `"capabilities": {"module_ota": {"backends": [...]}}`, where backends ⊆ `["dfu", "sim"]`. The key is omitted/empty when there is no backend.
- Check response gets the new field `artifact` ∈ `dfu|sim`. For sim, `download_url` = `reverse("module_firmware_api:download", args=[pk]) + "?artifact=sim"`, and checksum/size are the sim ones.
- Constants (agent, `module_ota.py`): `DETACH_WAIT_S = 3.0`, `SETTLE_S = 45.0` (dfu only; sim 0), `VERIFY_TIMEOUT_S = 180.0`, `VERIFY_POLL_S = 5.0`, `DFU_DOWNLOAD_TIMEOUT_S = 120`, `DFU_MAX_RETRIES = 2`, `NUDGE_MIN_INTERVAL_S = 10.0`.
- Agent config additions (dataclass **and** `load_config` mapping): `module_ota_enabled: bool = True`, `module_fw_dir: str = "/var/lib/station-agent/module-fw"`, `slots_conf: str = "/etc/oe5xrx/slots.conf"`, `sim_dir: str = "/var/lib/oe5xrx/sim"`, `sim_service: str = "oe5xrx-sim-harness"`, `dfu_util: str = "dfu-util"`.
- Classification (spec wins):
  - `rejected` only for a local artifact mismatch (variant of the slot's module ≠ instruction, or the instruction's artifact backend is unavailable) or a device-reported DFU rejection (`dfuERROR` in dfu-util output).
  - SHA mismatch, slot absent, uid mismatch and download failure are `failed` (transient).
  - A module that comes back with any non-target version, or does not come back within 180 s, is `rolled_back`.
- One flash per station: the module_ota cycle runs only on the main thread, sequentially.

## Review Focus

1. **Two identical modules:** instruction `module_uid` ≠ uid read live from the slot → never flash, report `failed`. Test in A3 (`test_uid_mismatch_never_flashes`).
2. **Agent restarts mid-flash:** a marker with phase `flashing` must not lead to a second flash unless the device is still in DFU mode (max 2 re-downloads total, counted across restarts). Test in A4 (`test_dfu_retry_budget_persists_across_restart`).
3. **A lock held before drain started:** the holder keeps it, re-acquire by the holder still succeeds, nobody else gets it, and check returns 204 until release. Tests in S3/S4.
4. **Old agent / no sim artifact / station offline:** never drain, never hand out an instruction the agent can't execute. Tests in S3 (`test_no_drain_without_capability`, `test_sim_module_without_sim_artifact_no_drain_no_instruction`, `test_no_drain_when_offline`).
5. **Paused slot during flash:** the heartbeat inventory and control inventory still list the module (status `updating`) and never drop it, and broker commands return `updating` immediately without waiting on the device lock. Tests in A2.

---

## Server tasks

### Task S1: Agent capabilities on Station

**Files:**
- Modify: `apps/stations/models.py` (Station) + new migration in `apps/stations/migrations/`
- Modify: `apps/api/serializers.py` (`HeartbeatSerializer`), `apps/api/views.py` (`HeartbeatView.post`)
- Test: `tests/test_heartbeat_capabilities.py`

**Interfaces:**
- Produces:
  - `Station.agent_capabilities = models.JSONField(default=dict, blank=True)`.
  - `Station.module_ota_backends() -> set[str]`, which returns `{"dfu","sim"} ∩ set(agent_capabilities.get("module_ota", {}).get("backends", []))` and is robust to malformed JSON (returns an empty set).

- [ ] Step 1: Write the failing tests:
  - `test_heartbeat_stores_capabilities`: the heartbeat with `"capabilities": {"module_ota": {"backends": ["dfu"]}}` makes `station.agent_capabilities` equal that dict.
  - `test_heartbeat_without_capabilities_clears`: an old agent with no key ends with `{}`, so a downgraded agent never keeps a stale capability.
  - `test_module_ota_backends_filters_garbage`: `{"module_ota": {"backends": ["dfu", "x", 3]}}` gives `{"dfu"}`, and `{"module_ota": "bad"}` gives `set()`.
  - `test_capabilities_size_bounded`: a dict over 4 KiB serialized is a 400 validation error.
- [ ] Step 2: Run them and confirm they fail.
- [ ] Step 3: Implement the field, the migration, an optional `capabilities = serializers.DictField(required=False)` with a size check, and store it in the view (always assign, default `{}`).
- [ ] Step 4: Run them and confirm they pass. Commit `feat(stations): store agent capabilities from heartbeat`.

### Task S2: Sim artifact (import + storage + download)

**Files:**
- Modify: `apps/module_firmware/models.py` (`ModuleFirmwareRelease`) + migration; `apps/module_firmware/storage.py`; `apps/module_firmware/releases.py`; `apps/module_firmware/api_views.py` (`ModuleFirmwareDownloadView`)
- Test: `tests/module_firmware/test_sim_artifact.py`

**Interfaces:**
- Produces:
  - Release fields `sim_storage_key` (CharField, blank, default ""), `sim_sha256` (CharField(64), blank), `sim_size_bytes` (PositiveBigIntegerField, null), `sim_cosign_bundle_key` (CharField, blank).
  - Property `has_sim_artifact -> bool` (`bool(sim_storage_key)`).
  - `storage.sim_release_key(type_key, version, variant) -> str`, which is the same layout as `release_key` but with the suffix `.native_sim`; the sim bundle key is that plus `.bundle`.
  - `releases.parse_sim_assets(names, prefix) -> dict[variant, name]`, which matches `{prefix}.native_sim` (variant "") / `{prefix}-{variant}.native_sim` and only counts entries with a sibling `{name}.bundle`.
  - Download: `GET <pk>/download/?artifact=sim` streams the sim blob (same Range/authz code path). It returns 404 if the release has no sim artifact. Any other `artifact` value is 400; a missing value means dfu (unchanged).
  - Delete/cleanup paths that delete `storage_key` also delete the sim keys.

- [ ] Step 1: Write the failing tests:
  - `test_import_pulls_native_sim_when_present`: the fake GH release has `fm-sa818-vhf.signed.bin(+.bundle)`, `fm-sa818-vhf.native_sim(+.bundle)` and SHA256SUMS. The release ends with `has_sim_artifact`, and `sim_sha256` matches SUMS.
  - `test_import_without_native_sim_ok`: there is no sim asset, the job is READY and `sim_storage_key == ""`.
  - `test_import_native_sim_sha_mismatch_fails_job`: the job is FAILED and the uploads are cleaned up.
  - `test_import_native_sim_cosign_fail_fails_job`: `verify_blob_identity` raises for the sim blob, and the job is FAILED.
  - `test_native_sim_without_bundle_ignored`: there is no sim, and the job is READY.
  - `test_download_sim_artifact` (200 with sim bytes, Range works), `test_download_sim_missing_404`, `test_download_bad_artifact_400`, and `test_download_default_is_dfu`.
  - Follow the existing mocking style in `tests/module_firmware/test_import_release.py` and `test_fw_download.py`.
- [ ] Step 2: Run them and confirm they fail.
- [ ] Step 3: Implement. In `import_release_tag`, the sim blob is verified exactly like the signed.bin (SUMS sha + `verify_blob_identity` with the same identity) inside the same publish transaction. On re-import, `update_or_create` must also set/clear the sim fields.
- [ ] Step 4: Run them, plus `tests/module_firmware/`, and confirm they pass. Commit `feat(module_firmware): optional native_sim artifact (import, storage, download)`.

### Task S3: Drain predicate + idle-gated check with artifact selection

**Files:**
- Create: `apps/module_firmware/drain.py`
- Modify: `apps/module_firmware/reconcile_api_views.py` (`ReconcileCheckView`), `apps/module_firmware/reconcile_serializers.py` (response field `artifact`)
- Test: `tests/module_firmware/test_drain.py`, `tests/module_firmware/test_reconcile_check_gating.py`

**Interfaces:**
- Consumes: S1 `Station.module_ota_backends()`, S2 `has_sim_artifact`, sim fields.
- Produces (`drain.py`):
  - `artifact_for(module) -> str` returns `"sim"` if `module.uid_source == Module.UidSource.SYNTHETIC` else `"dfu"`. Check the actual enum name in models.
  - `artifact_available(release, artifact) -> bool`: dfu → `bool(release.storage_key)`, sim → `release.has_sim_artifact`.
  - `pending_updates(station) -> list[ModuleFirmwareConvergenceState]`: rows in state UPDATING for modules open-assigned to `station`, whose `target_release` equals `desired_release_for_module(module)`, whose `artifact_for(module) in station.module_ota_backends()` and whose `artifact_available(...)` holds. Ordered by slot. Read-only (no `reconcile_module` call, no locks).
  - `@dataclass(frozen=True) DrainStatus(active: bool, since: datetime | None, module_uids: tuple[str, ...])`.
  - `drain_status(station) -> DrainStatus`: `active = station.is_online and bool(pending)` (adapt to whether `is_online` is a property). `since` is the min `created_at` of the pending rows.
  - `is_draining(station) -> bool`.
- Check view changes:
  - The station's ControlLock (any scope) has `holder_id` set → 204 (idle gate). Evaluate this **before** reconciling.
  - In the candidate loop, skip modules whose artifact isn't in the backends or isn't available.
  - The instruction carries `artifact`, plus `download_url`/`checksum_sha256`/`size_bytes` of that artifact.

- [ ] Step 1: Write the failing tests:
  - Drain: `test_drain_active_for_pending_dfu`, `test_no_drain_without_capability` (backends empty), `test_no_drain_when_offline` (last_seen 10 min ago), `test_no_drain_when_quarantined`, `test_no_drain_when_converged`, `test_sim_module_without_sim_artifact_no_drain_no_instruction`, `test_drain_since_is_oldest_pending_created_at`, and `test_stale_row_for_old_target_ignored` (an UPDATING row for a release that is no longer desired does not drain).
  - Check: `test_check_204_while_lock_held`, `test_check_returns_instruction_after_release`, `test_check_sim_module_gets_sim_url_and_checksum` (`artifact == "sim"`, url ends `?artifact=sim`, checksum = `sim_sha256`), `test_check_real_module_gets_dfu` (`artifact == "dfu"`) and `test_check_skips_module_whose_backend_missing` (204).
  - Existing `test_reconcile_check*.py` must keep passing. Fix their fixtures by giving their stations `agent_capabilities={"module_ota": {"backends": ["dfu"]}}` where needed.
- [ ] Step 2: Run them and confirm they fail.
- [ ] Step 3: Implement.
- [ ] Step 4: Run `tests/module_firmware/` and confirm it passes. Commit `feat(module_firmware): drain predicate, idle gate and artifact selection in reconcile check`.

### Task S4: Drain enforcement in ControlLock + UI signal

**Files:**
- Modify: `apps/control/lock.py`, `apps/control/consumers.py` (`_lock_acquire`, `_lock_request`, `_lock_transfer`, `_lock_status`/`_push_lock`)
- Test: `tests/test_control_lock_drain.py`, `tests/test_control_consumer_drain.py`

**Interfaces:**
- Consumes: S3 `drain.is_draining(station)`, `drain.drain_status(station)`.
- Produces:
  - `lock.acquire(...)` returns False when the lock is free (or held by another user) and the station is draining. Re-acquire by the **current holder** stays True.
  - `request_control` returns None and does nothing while draining.
  - `transfer` returns False while draining.
  - `preempt`/`release`/`touch`/sweeps are unchanged.
  - Import `drain` lazily inside the functions to avoid an import cycle.
  - `lock.FIRMWARE_DRAIN_MESSAGE = "Firmware-Update ausstehend — neue Steuerung ist gesperrt, bis das Update abgeschlossen ist."`
  - Consumer: when acquire/request/transfer is refused due to drain, it sends `{"type":"error","code":"firmware_update_pending","message": FIRMWARE_DRAIN_MESSAGE}`.
  - The lock frame payload gains `"drain": {"since": iso8601} | null` (computed in `_lock_status`).

- [ ] Step 1: Write the failing tests:
  - Lock (unit, patch `drain.is_draining`): `test_acquire_refused_while_draining`, `test_holder_reacquire_allowed_while_draining`, `test_request_refused_while_draining`, `test_transfer_refused_while_draining`, `test_release_and_preempt_unaffected`.
  - Consumer (WebsocketCommunicator, copy setup from `tests/test_control_consumer_lock.py`): `test_acquire_while_draining_sends_firmware_update_pending_error`, `test_lock_frame_carries_drain_since`, `test_lock_frame_drain_null_when_idle`.
- [ ] Step 2: Run them and confirm they fail.
- [ ] Step 3: Implement.
- [ ] Step 4: Run `tests/test_control*.py` and confirm it passes. Commit `feat(control): refuse new control grants while a firmware update is pending`.

### Task S5: firmware_check nudge

**Files:**
- Create: `apps/module_firmware/nudge.py`
- Modify: `apps/module_firmware/services.py` (`reconcile_module_type`), `apps/module_firmware/releases.py` (after a successful publish: `reconcile_module_type(module_type)` via `transaction.on_commit`)
- Test: `tests/module_firmware/test_nudge.py`

**Interfaces:**
- Produces: `nudge.nudge_stations(station_ids: Iterable[int]) -> None`.
  - It registers `transaction.on_commit` and sends the frame `{"v":1,"type":"firmware_check"}` to each `control_{id}_agent` group via `async_to_sync(channel_layer.group_send)`.
  - It deduplicates ids, and exceptions are logged, never raised.
  - `reconcile_module_type` collects the station ids of reconciled modules whose resulting state is UPDATING and nudges them.

- [ ] Step 1: Write the failing tests (in-memory channel layer; `django_capture_on_commit_callbacks(execute=True)`):
  - `test_target_create_nudges_affected_station` (via `reconcile_module_type`), `test_no_nudge_for_converged_station`, `test_nudge_dedup` and `test_channel_layer_failure_swallowed`.
  - `test_import_publish_triggers_reconcile_and_nudge`.
- [ ] Step 2: Run them and confirm they fail.
- [ ] Step 3: Implement.
- [ ] Step 4: Run them and confirm they pass. Commit `feat(module_firmware): nudge agents to re-check on target/release change`.

### Task S6: "Firmware-Update läuft" + "Station wartet seit …" UI

**Files:**
- Modify: `apps/control/models.py` (`StationModule.firmware_updating = BooleanField(default=False)`) + migration; `apps/control/registry.py` (`apply_inventory`); `apps/control/serializers.py` (snapshot); `apps/control/consumers.py` (agent `event` branch)
- Modify: `static/js/control-panel.js` (`_ingestInventory`, `_onEvent`, `_onError`, lock handling), `apps/control/templates/control/_module_card.html`, `apps/control/templates/control/panel.html`, `apps/stations/templates/stations/station_detail.html` (+ its view context)
- Test: `tests/test_control_firmware_updating.py`, `tests/js/` (follow the existing JS test harness, if `control-logic.js` helpers are tested there)

**Interfaces:**
- Consumes: S3 `drain.drain_status`, S4 lock-frame `drain`, the agent event names from Global Constraints.
- Produces:
  - `apply_inventory`: a module entry with `status == "updating"` sets `firmware_updating=True`, keeps it online, and **skips `ingest_module`** for it (no identity/version churn, no audits). Every other entry sets `firmware_updating=False`.
  - Agent consumer: `event` with `firmware_update_started` sets the flag True and `firmware_update_finished` sets it False (on the StationModule with that slot/module_id), then the event is forwarded as before.
  - The snapshot includes `"updating": bool` per module.
  - JS: `this.updating[mkey]` comes from the inventory `status`/snapshot `updating` and the events. The card shows the banner „Firmware-Update läuft" and all controls of that module are disabled.
  - The `firmware_update_pending` error shows the message as a notice.
  - The lock frame `drain.since` renders the station banner „Station wartet auf Firmware-Update seit <lokale Zeit>". The same banner is rendered server-side on `panel.html` and `station_detail.html` from `drain_status(station)`.
  - `firmware_dfu_stuck` sets `errors[mkey]` to detail.msg.
- Pixel work: invoke `Skill("frontend-design")` and match the existing pill/banner styles.

- [ ] Step 1: Write the failing tests:
  - `test_inventory_updating_sets_flag_and_skips_ingest` (patch `ingest_module`, assert it is not called for the entry), `test_inventory_normal_clears_flag`, `test_started_finished_events_toggle_flag`, `test_snapshot_includes_updating`, `test_station_detail_shows_drain_banner` and `test_control_panel_shows_drain_banner`.
- [ ] Step 2: Run them and confirm they fail.
- [ ] Step 3: Implement the server and JS/templates.
- [ ] Step 4: Run `python -m pytest -q tests/test_control*.py tests/test_station*.py` and the JS tests, and confirm they pass. Commit `feat(control): show firmware update in progress and drain wait state`.

---

## Agent tasks

### Task A1: Config, pause registry, capability report

**Files:**
- Modify: `station_agent/config.py` (fields from Global Constraints + `load_config` mapping)
- Create: `station_agent/slot_pause.py`
- Create: `station_agent/module_ota.py` (only `detect_backends` in this task)
- Modify: `station_agent/heartbeat.py` (`collect_system_info` adds `capabilities`)
- Test: `tests/test_slot_pause.py`, `tests/test_module_ota_capabilities.py`

**Interfaces:**
- Produces:
  - `slot_pause`: module-level registry, thread-safe (`threading.Lock`).
    - `pause(slot: int, snapshot: dict | None) -> None`, where the snapshot is the slot's last-known inventory entry `{"slot", "control", "modules":[...]}`.
    - `resume(slot: int) -> None`.
    - `is_paused(slot: int) -> bool`.
    - `paused_slots() -> frozenset[int]`.
    - `updating_entry(slot: int) -> dict | None` returns a deep copy of the snapshot with `"status": "updating"` set on each module dict.
    - `_reset_for_tests()`.
  - `module_ota.detect_backends(config) -> list[str]`:
    - `"dfu"` if `shutil.which(config.dfu_util)` and `os.path.isfile(config.slots_conf)`.
    - `"sim"` if `os.path.isdir(config.sim_dir)`.
    - `[]` if `module_ota_enabled` is False.
  - Heartbeat: `capabilities = {"module_ota": {"backends": b}}` when `b`, else `{}`.

- [ ] Step 1: Write the failing tests: pause/resume/is_paused and the updating_entry deep-copy (mutating the result never mutates the stored snapshot), plus threaded hammering. Also `test_detect_backends_dfu`, `_sim`, `_none`, `_disabled` (using tmp files and monkeypatched `shutil.which`) and `test_heartbeat_includes_capabilities`.
- [ ] Step 2: Run them and confirm they fail. Step 3: Implement. Step 4: Run them and confirm they pass. Commit `feat(agent): module_ota capability detection and slot pause registry`.

### Task A2: Honour the pause registry + cross-thread hooks

**Files:**
- Modify: `station_agent/slot_discovery.py` (`discover_slots(..., skip: Collection[int] = ())`), `station_agent/inventory.py` (`_collect_modules`), `station_agent/control_client.py` (`_SlotInventoryDebouncer`, `_rediscovery_loop`, ControlClient), `station_agent/broker.py`, `station_agent/audio/engine.py`, `station_agent/audio/ws_client.py`
- Test: `tests/test_slot_pause_integration.py`

**Interfaces:**
- Consumes: A1 `slot_pause`.
- Produces:
  - `discover_slots` skips slot numbers in `skip` without opening them.
  - `_collect_modules` and the control path pass `slot_pause.paused_slots()` as skip, then add `slot_pause.updating_entry(slot)` for each paused slot that has a snapshot (sorted by slot).
  - `_SlotInventoryDebouncer.update` never counts a miss for a paused slot and returns its `updating_entry`.
  - `Broker._execute` on a paused slot returns `{"ok": False, "error": "updating"}` immediately (no `_slot_locks`, no executor).
  - `emit_inventory` skips the state `get`s for paused slots (state `{}`).
  - `Broker.slot_busy(slot: int) -> bool` is True if any `(slot, _)` is in `self._ptt`.
  - `Broker.handle` calls `self._on_firmware_check()` (a constructor kwarg `on_firmware_check: Callable[[], None] | None = None`) for `type == "firmware_check"`.
  - `AudioEngine.slot_active(slot: int) -> bool` is True if `f"slot{slot}.rx" in self._rx`, or `self._tx` or `self._diag` has `["slot"] == slot`.
  - `AudioClient.slot_active(slot) -> bool` returns False when there is no engine.
  - `ControlClient(config, virtual_modules=None, on_firmware_check=None)` keeps `self._broker` (set/cleared per connection) and adds:
    - `slot_busy(slot) -> bool`, which is thread-safe: `asyncio.run_coroutine_threadsafe` to the loop with a timeout of 2 s, and a timeout or no loop means True (fail closed = busy).
    - `send_event(slot, module, event, detail) -> None`, which is fire-and-forget threadsafe and a no-op when disconnected.
    - `request_inventory_refresh() -> None`, which is threadsafe and makes the rediscovery loop run one immediate cycle (an `asyncio.Event` checked alongside the sleep) and emit if changed.
- Also: `slot_pause.pause` snapshot source — `ControlClient.last_inventory_entry(slot) -> dict | None` returns the latest effective (debounced) entry for that slot, thread-safe via a lock-protected copy.

- [ ] Step 1: Write the failing tests:
  - `test_heartbeat_inventory_keeps_paused_slot_as_updating`: the fake_fw slot tree with slot 1 is paused with a snapshot, `collect_inventory()["modules"]` contains slot 1 with `status == "updating"`, and the fw saw no `module list`.
  - `test_debouncer_never_drops_paused_slot` (10 consecutive misses), `test_broker_execute_paused_returns_updating_fast` (< 0.1 s, the device lock is held by the test thread), `test_broker_slot_busy_tracks_ptt`, `test_broker_firmware_check_calls_callback`, `test_audio_engine_slot_active`, `test_control_client_slot_busy_fail_closed_without_loop`, and `test_request_inventory_refresh_emits_updating_status`. The last one uses the existing websockets test server from `tests/test_control_client.py`.
- [ ] Step 2: Run them and confirm they fail. Step 3: Implement. Step 4: Run them, plus the full `tests/test_control_client.py tests/test_broker*.py tests/test_inventory*.py tests/test_slot*.py tests/test_heartbeat*.py`, and confirm they pass. Commit `feat(agent): pause registry honoured by inventory, debouncer, broker`.

### Task A3: module_ota core cycle (backend-agnostic)

**Files:**
- Modify: `station_agent/module_ota.py`
- Test: `tests/test_module_ota_cycle.py` (FakeHttp records POSTs; FakeBackend; fake `read_version`)

**Interfaces:**
- Consumes: A1/A2 (`slot_pause`, `ControlClient.slot_busy/send_event/request_inventory_refresh/last_inventory_entry`, `AudioClient.slot_active`), `ota.download_firmware_resumable(http_client, url, sha, dest, expected_size=...)`.
- Produces:
  - `read_slot_identity(config, slot: int, module_uid: str, timeout=3.0) -> dict | None`:
    - It calls `slot_discovery.probe_slot(f"{config.slot_dev_base}/slot{slot}/control", timeout)` (which takes the devlock itself).
    - It returns the `identity` of the module whose `identity["uid"] == module_uid`, else None.
    - The version is `identity["version"]`.
  - `class FlashOutcome(enum.Enum)`: `OK`, `REJECTED`, `TRANSIENT`, `ROLLED_BACK`, `DFU_STUCK`.
  - Backend protocol:
    - `name: str` (`"dfu"`/`"sim"`) and `settle_s: float`.
    - `flash(slot: int, path: str, marker: Marker) -> FlashOutcome`. It returns OK when the image was handed to the device and verification may begin, REJECTED/TRANSIENT/DFU_STUCK otherwise. It may update `marker.dfu_attempts` and call `marker.save()`.
    - `after_verify(slot: int, success: bool) -> None`. The sim backend uses it for `rm pending` / rollback.
    - `recover(marker: Marker) -> FlashOutcome | None`. This is the startup hook; None means "fall through to version classification".
  - `@dataclass Marker`:
    - Fields: `convergence_id: int`, `slot: int`, `module_uid: str`, `target_version: str`, `old_version: str`, `artifact: str`, `path: str`, `phase: str` (`"downloaded"|"flashing"`), `dfu_attempts: int = 0`, `started_at: float`.
    - `save()` writes JSON atomically to `state_dir/module_ota.json` (temp + fsync + rename); `Marker.load(state_dir) -> Marker | None` tolerates corrupt files (log, delete, None); `clear()`.
  - `class ModuleOta(config, http_client, control=None, audio=None, backends: dict[str, Backend] | None = None, read_identity=read_slot_identity, clock=time.monotonic, sleep=time.sleep)`:
    - `run_once() -> str | None` returns the result string or None for nothing done. It does nothing if `detect_backends` is empty.
    - `recover_on_startup() -> str | None`.
    - `run_once` flow:
      1. If a marker exists → recover (`_finish_from_marker`).
      2. `POST /api/v1/module-firmware/reconcile/check/` `{}`. 204/None → None.
      3. Parse/validate the instruction. A malformed one is logged and dropped.
      4. `artifact` not in backends → status `rejected`.
      5. Live identity None or uid mismatch → `failed` ("module not found in slot"). Variant ≠ instruction variant (both non-empty) → `rejected`. Live version == target → commit directly (already converged).
      6. Idle gate: `control.slot_busy(slot)` or `audio.slot_active(slot)` → return `"busy"` with **no** POST.
      7. Pause the slot with the snapshot from `control.last_inventory_entry(slot)` (fallback: build it from the live identity). Then `send_event(started)` and `request_inventory_refresh()`.
      8. Status `downloading`, then download to `module_fw_dir/conv{id}-{target}.{bin|native_sim}`. Failure → `failed`.
      9. `Marker(phase="downloaded").save()`, then status `flashing`, then `marker.phase="flashing"; save()`, then `backend.flash`.
      10. OK → status `verifying`, then `_verify`.
      11. `finally`: resume the slot, `send_event(finished, result)`, `request_inventory_refresh()`, delete the downloaded file, clear the marker.
    - `_verify(marker, backend) -> str` (shared by `run_once` and recovery):
      - Poll `read_identity` every `VERIFY_POLL_S` until `VERIFY_TIMEOUT_S` (measured from the flash return).
      - A version is only trusted after `backend.settle_s` has elapsed.
      - `== target` → `backend.after_verify(slot, True)`, then `POST reconcile/commit/` `{"convergence_id", "version"}` → `"success"` (a 409 → `"rolled_back"`).
      - Any other version after settle, or timeout → `backend.after_verify(slot, False)`, then status `rolled_back` with `error_message` naming the observed version or "module did not come back within 180 s".
    - Recovery from marker:
      - Phase `downloaded` → the live version is never compared as a failure: if it equals target, commit; otherwise status `failed` ("agent restarted before flashing").
      - Phase `flashing` → `backend.recover(marker)`. An outcome other than None is reported. None → `_verify`.
      - The slot is paused for the duration of recovery as well.
    - All status/commit POSTs are best-effort. A failure or 409 is logged, and the marker is still cleared at the end, because the server derives state from the reported version.
    - Outcome → status mapping: REJECTED→`rejected`, TRANSIENT→`failed`, ROLLED_BACK→`rolled_back`, DFU_STUCK→`rolled_back` + `send_event("firmware_dfu_stuck", {"msg": ...})` + error_message with the same text.

- [ ] Step 1: Write the failing tests:
  - `test_success_path_posts_downloading_flashing_verifying_then_commit`.
  - `test_no_instruction_no_posts`.
  - `test_busy_ptt_skips_without_status`, `test_busy_audio_skips_without_status`.
  - `test_uid_mismatch_never_flashes` (backend.flash not called, status failed).
  - `test_variant_mismatch_rejected`, `test_artifact_backend_missing_rejected`.
  - `test_sha_mismatch_failed_and_file_removed`.
  - `test_old_version_after_settle_rolled_back`; `test_version_before_settle_not_trusted` (target is seen only before settle and old after → rolled_back); `test_timeout_rolled_back` (fake clock).
  - `test_commit_409_reported_as_rolled_back`.
  - `test_slot_paused_during_flash_and_resumed_after` (also on exception).
  - `test_events_started_finished`.
  - `test_marker_written_before_flash_and_cleared_after`.
  - `test_recover_phase_downloaded_reports_failed_not_rolled_back`; `test_recover_phase_flashing_classifies_target_as_success`.
  - `test_corrupt_marker_ignored`.
  - `test_already_on_target_commits_without_flash`.
- [ ] Step 2: Run them and confirm they fail. Step 3: Implement. Step 4: Run them and confirm they pass. Commit `feat(agent): module_ota reconcile cycle with crash marker`.

### Task A4: DFU backend

**Files:**
- Modify: `station_agent/module_ota.py` (or create `station_agent/module_ota_dfu.py` if `module_ota.py` > ~400 lines)
- Test: `tests/test_module_ota_dfu.py` (fake `dfu-util` = executable script in tmp_path writing argv to a log and behaving per env/state file)

**Interfaces:**
- Consumes: A3 Backend protocol, Marker, FlashOutcome; `devlock.control_device_lock`.
- Produces:
  - `parse_slots_conf(path) -> dict[int, str]`: lines `slot<N>=<port>`, ignore `#`/blank, ignore malformed lines with a warning, and validate port against `^\d+(-\d+)(\.\d+)*$`.
  - `class DfuBackend(config, run=subprocess.run, sleep=time.sleep)` with `name="dfu"` and `settle_s=SETTLE_S`.
    - `flash`:
      1. Port lookup. A missing slot gives TRANSIENT.
      2. Under `control_device_lock(control_path, timeout=15)` (TimeoutError → TRANSIENT): `[dfu_util, "-e", "-p", port]` (check=False, timeout 15), then sleep `DETACH_WAIT_S`.
      3. Release the lock, then run the download loop.
    - Download loop: `[dfu_util, "-a", "0", "-p", port, "-D", path]` (timeout `DFU_DOWNLOAD_TIMEOUT_S`, capture output).
      - rc 0 → OK.
      - Output contains `dfuERROR` → REJECTED.
      - Otherwise, if `in_dfu_mode(port)` and `marker.dfu_attempts < DFU_MAX_RETRIES`: `marker.dfu_attempts += 1; marker.save()` and retry.
      - Otherwise, if `in_dfu_mode(port)` → DFU_STUCK.
      - Otherwise → TRANSIENT (device not found; it never entered DFU, the module still runs the old FW).
      - `subprocess.TimeoutExpired`/`OSError` are treated like a non-zero rc.
    - `in_dfu_mode(port) -> bool`: `[dfu_util, "-l", "-p", port]` output contains `Found DFU`.
    - `recover(marker)`: if `in_dfu_mode(port)` → run the same download loop (sharing the persisted `dfu_attempts` budget). Its OK → None (verify follows), else that outcome. Not in DFU mode → None.
    - `after_verify` is a no-op.
  - Register `DfuBackend` in `ModuleOta`'s default backends when `"dfu"` is detected.

- [ ] Step 1: Write the failing tests:
  - `test_parse_slots_conf` (comments, blanks, malformed, bad port).
  - `test_flash_commands_use_port_path_never_vid_pid` (argv contains `-p 1-1.2` and never `-d`).
  - `test_dfu_error_status_is_rejected`.
  - `test_device_not_found_is_transient`.
  - `test_dfu_hang_retries_twice_then_stuck` (3 download invocations total, DFU_STUCK).
  - `test_dfu_hang_recovers_on_retry` (2nd download rc 0 → OK).
  - `test_dfu_retry_budget_persists_across_restart`: every download after the first is a retry (`dfu_attempts += 1; marker.save()` BEFORE it); in `recover` every download is a retry. Marker `dfu_attempts=2` + device in DFU mode → recover does NO download and returns DFU_STUCK; `dfu_attempts=1` → exactly one download, then DFU_STUCK if it fails. Downloads across initial run + restart never exceed 3.
  - `test_recover_not_in_dfu_falls_through` (None, no download).
  - `test_detach_holds_devlock` (another thread can't acquire during detach).
  - `test_download_timeout_treated_as_failure`.
  - E2E with A3: `test_module_ota_dfu_stuck_reports_rolled_back_and_event`.
- [ ] Step 2: Run them and confirm they fail. Step 3: Implement. Step 4: Run them and confirm they pass. Commit `feat(agent): dfu-util backend with port-path addressing and DFU-hang retry`.

### Task A5: Sim backend (linux-image contract)

**Files:**
- Modify/Create: `station_agent/module_ota.py` or `station_agent/module_ota_sim.py`
- Test: `tests/test_module_ota_sim.py` (tmp `sim_dir`, fake `systemctl` runner recording calls; ownership/chown tolerated when not root — wrap `os.chown` so tests monkeypatch it)

**Interfaces:**
- Consumes: A3 protocol.
- Produces: `class SimBackend(config, run=subprocess.run)` with `name="sim"` and `settle_s=0.0`. The contract is copied verbatim (`D = config.sim_dir`):
  - `flash` (steps 1–5):
    1. Copy the downloaded file to `D/native-sim-fm.new`, then fsync, `chown root:root`, `chmod 0755`, and verify that the sha256 equals the source.
    2. If `D/native-sim-fm` exists: `os.link` it to `D/native-sim-fm.prev`, replacing it (link to a temp name then rename, or unlink+link). Else: `rm -f D/native-sim-fm.prev`.
    3. `touch D/pending` + fsync the dir.
    4. `os.replace(D/native-sim-fm.new, D/native-sim-fm)`.
    5. `systemctl restart <sim_service>`.
    - It returns OK, or TRANSIENT on any OSError/non-zero restart **before step 3**. A failure after step 3 runs the rollback and returns ROLLED_BACK.
  - `after_verify(slot, True)` → `rm -f D/pending`. `after_verify(slot, False)` → `rollback()`.
  - `rollback()` (idempotent):
    - R1: if `.prev` exists, `os.link(.prev, .rb)` (replace an existing `.rb`) then `os.replace(.rb, native-sim-fm)`; else `rm -f native-sim-fm`.
    - R2: `systemctl restart` (always).
    - R3: `rm -f pending native-sim-fm.new native-sim-fm.rb` with `pending` **last**.
  - `recover(marker)`: if `D/pending` exists → `rollback()` → ROLLED_BACK. Else → None.

- [ ] Step 1: Write the failing tests:
  - `test_flash_order_prev_before_pending_before_rename` (record the op order through a wrapped fs layer or by checking the state at the restart call).
  - `test_flash_first_time_removes_stale_prev`.
  - `test_prev_is_hardlink_same_inode`.
  - `test_success_removes_pending_keeps_new_binary`.
  - `test_failure_rolls_back_to_prev`, `test_failure_without_prev_removes_override`.
  - `test_rollback_idempotent_rerun` (interrupt after R1, then rerun).
  - `test_recover_with_pending_rolls_back`, `test_recover_without_pending_falls_through`.
  - `test_restart_failure_before_pending_is_transient`.
  - `test_pending_removed_last_in_rollback`.
- [ ] Step 2: Run them and confirm they fail. Step 3: Implement. Step 4: Run them and confirm they pass. Commit `feat(agent): native_sim flash backend per linux-image contract`.

### Task A6: Wire into agent main loop

**Files:**
- Modify: `station_agent/agent.py`
- Test: `tests/test_agent_module_ota_wiring.py`

**Interfaces:**
- Consumes: A2 ControlClient kwargs/methods, A3 `ModuleOta`.
- Produces:
  - `StationAgent._wake = threading.Event()`; `_handle_signal` also sets `_wake`.
  - `ControlClient(..., on_firmware_check=self._wake.set)`.
  - `self._module_ota = ModuleOta(config, http_client, control=control_client, audio=audio_client)` is built after the threads start. `recover_on_startup()` runs once before the loop, inside try/except.
  - Main loop:
    - Wait with `self._wake.wait(timeout=heartbeat_interval)`.
    - If woken by a nudge (not shutdown): `clear()`, then run `module_ota.run_once()` if ≥ `NUDGE_MIN_INTERVAL_S` has passed since the last run, and `continue` without an extra heartbeat.
    - On the regular cadence, run `run_once()` right after `_handle_ota`, in the same `heartbeat_count % ota_check_interval == 0` branch.
    - Every call is wrapped in try/except logging.
  - Image OTA and module OTA never overlap. Both run sequentially on the main thread.

- [ ] Step 1: Write the failing tests:
  - `test_nudge_triggers_module_ota_run`.
  - `test_nudge_rate_limited`.
  - `test_shutdown_wakes_loop_and_exits`.
  - `test_recover_on_startup_called_once`.
  - `test_module_ota_exception_does_not_kill_loop`.
  - Patch `send_heartbeat`, ControlClient and AudioClient as `tests/test_agent_ota_flow.py` does.
- [ ] Step 2: Run them and confirm they fail. Step 3: Implement. Step 4: Run the full suite `python -m pytest -q` + ruff and confirm it passes. Commit `feat(agent): run module_ota on cadence and firmware_check nudge`.

---

## Execution order / parallelism (fan-out ≤ 2)

- Pair 1: **S1** ∥ **A1**
- Pair 2: **S2** ∥ **A2**
- Pair 3: **S3** ∥ **A3**
- Pair 4: **S4** ∥ **A4**
- Pair 5: **S5** ∥ **A5**
- Pair 6: **S6** ∥ **A6**

Server and agent touch disjoint files. Within a stream the tasks are sequential.
