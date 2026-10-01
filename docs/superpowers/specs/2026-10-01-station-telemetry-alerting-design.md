# Station-Telemetrie + Alerting (Strang 2+3) — Design

**Date:** 2026-10-01
**Feature branch:** `feature/station-telemetry-alerting` (implemented on `sub/dbbad7b4`, PR → `main`)
**Author:** Child session dbbad7b4 (coordinator a05e1114)

## Motivation

Today the station-manager learns far too little about its stations. A station
that reboots in a loop, browns out on undervoltage, or wears out its eMMC is
invisible until someone notices the symptom. This feature closes that gap in two
strands:

- **Strang 2 — Telemetry:** `station_agent` collects richer health/boot/power/
  storage/slot data and ships it in the existing heartbeat; the server stores it
  and surfaces it on the station detail page.
- **Strang 3 — Alerting:** the `monitoring` app actively notifies the right
  people (via the existing topology-routed EMAIL/PUSH/Telegram channels) on:
  station offline, unexpected reboot, OTA fail/rollback, and power/health
  warnings.

## Binding interface contract

This design implements the **CONSUMER** side of
`contract/station-telemetry-interface` (coordinator-owned). The parallel
`linux-image` feature (`feature/image-debug-observability`) is the **PROVIDER**
of the OS-side sources. Hard rule from the contract: **every source is
feature-detected and gracefully degraded** (qemu / pre-merge / SD-instead-of-eMMC
/ non-rpi) — the agent never hard-fails because a source is missing. A missing
source means the corresponding field is omitted or `null`, never an exception.

### Sources (contract)

| Signal | Source | Provided by | Degrade when absent |
|---|---|---|---|
| Kernel crash capture | `/sys/fs/pstore/dmesg-ramoops-*` | A (pstore on) | no record ⇒ not a crash |
| Boot separation | `journalctl --list-boots` / `-b -1` | A | skip prior-boot diagnostics |
| Undervoltage/throttle | `vcgencmd get_throttled` (bitmask) | A (vcgencmd on PATH) | binary missing (qemu) ⇒ omit `power` |
| Storage health | `mmc extcsd read /dev/mmcblkX` (mmc-utils) + dmesg `mmc`/`I/O error` scan | A (mmc-utils) | SD ⇒ life/pre-eol `n/a`; no mmc binary ⇒ omit extcsd, keep dmesg scan |
| Active slot | `/proc/cmdline` `root=PARTLABEL=root_[ab]` | already present | `RuntimeError` already handled upstream |
| Image version | `/etc/os-release` `OE5XRX_RELEASE` | already present | empty string |
| Bootloader env | `fw_printenv boot_part/bootcount/upgrade_available` | already present | `None` (existing 10s timeout/OSError handling) |
| uptime / disk / RAM / CPU temp | `/proc/uptime`, `df`/statvfs, `/proc/meminfo`, thermal sysfs | already present | existing behaviour |

## Architecture overview

```
station_agent (B)                         station-manager server
─────────────────                         ──────────────────────
telemetry.collect_telemetry(config)
  ├─ boot.detect_boot(state_dir)   ──┐
  ├─ power.read_throttle()           │   POST /api/v1/heartbeat/
  ├─ storage.read_storage_health()   ├──▶ HeartbeatView
  └─ slot/version/ota (existing)     │     ├─ HeartbeatSerializer(+telemetry)
                                     │     ├─ ingest.ingest_telemetry(station, data)
clean-shutdown marker on SIGTERM ────┘     │     ├─ StationTelemetry (snapshot + columns)
                                           │     ├─ StationAuditLog REBOOT event on boot_id change
                                           │     └─ broadcast_station_status (existing)
                                           │
                                           └─▶ monitoring.engine.check_alerts() (periodic cmd)
                                                 ├─ _check_station_offline      (existing)
                                                 ├─ _check_ota_failed           (existing)
                                                 ├─ _check_unexpected_reboot    (NEW)
                                                 ├─ _check_power_warning        (NEW)
                                                 └─ _check_storage_health       (NEW)
                                                       └─▶ send_alert_notifications (existing, topology-routed)
```

## Strang 2 — Agent telemetry

### Heartbeat payload: new top-level `telemetry` block

Telemetry is added as a **new top-level key** `telemetry` in the heartbeat
payload (sibling to the existing `inventory`), not merged into `inventory`. This
keeps the contract explicit and lets the server treat telemetry as a separate,
versioned concern. `collect_system_info()` gains `"telemetry": collect_telemetry(config)`.

```json
"telemetry": {
  "boot": {
    "boot_id": "e3b0c442-...",          // /proc/sys/kernel/random/boot_id
    "boot_count": 42,                     // agent-maintained persistent counter
    "uptime_seconds": 12345.6,
    "reboot_reason": "clean|crash|watchdog|ota_rollback|undervoltage|unknown"
  },
  "power": {                              // omitted entirely on non-rpi / no vcgencmd
    "throttled_hex": "0x50005",
    "undervoltage_now": false,
    "undervoltage_occurred": true,
    "throttled_now": false,
    "throttled_occurred": true,
    "freq_capped_now": false,
    "freq_capped_occurred": false
  },
  "slot": {
    "active_slot": "a",                  // null if undeterminable
    "image_version": "v1.2.3",           // OE5XRX_RELEASE, "" if absent
    "last_ota_result": "success|rolled_back|failed|null",
    "last_ota_detail": ""
  },
  "storage": {
    "root_device": "mmcblk0",            // null if undeterminable
    "devices": [
      {
        "name": "mmcblk0",
        "kind": "emmc|sd|unknown",
        "life_time_a_pct": 10,            // null for SD / no extcsd
        "life_time_b_pct": 0,             // null for SD / no extcsd
        "pre_eol": "normal|warning|urgent|n/a",
        "io_error_count": 0               // dmesg mmc/I-O error count (both SD+eMMC)
      }
    ]
  }
}
```

**Every sub-block and every field is independently optional.** A collector that
cannot run returns `None`/omits its key; `collect_telemetry` assembles whatever
succeeded. One failing collector never aborts the others or the heartbeat.

### New agent modules

- **`station_agent/telemetry.py`** — `collect_telemetry(config) -> dict`.
  Orchestrates the sub-collectors, each wrapped so an exception degrades to an
  omitted field (logged at debug). Returns `{}` only if literally nothing could
  be collected (still valid — server stores empty).

- **`station_agent/bootinfo.py`** — boot/reboot-reason logic (**owner = agent**
  per contract). Public:
  - `read_boot_id()` → `/proc/sys/kernel/random/boot_id` (None if absent).
  - `detect_boot(state_dir, bootloader) -> dict` — reads persisted
    `{boot_id, boot_count}` from `<state_dir>/boot_state.json`; if the current
    `boot_id` differs (or no state), this is a **new boot**: compute
    `reboot_reason` for the boot that just ended, increment `boot_count`, persist
    the new state, and consume one-shot evidence (delete pstore records, clear
    clean-marker). If `boot_id` unchanged, return the cached reason/count without
    recomputing. Degrades to `boot_count=0`/`reason="unknown"` if `state_dir` is
    unwritable.
  - `compute_reboot_reason(...)` — pure function over the evidence inputs, so it
    is unit-testable with fixtures. Priority:
    1. `pstore` has a fresh `dmesg-ramoops-*` record ⇒ **crash**
    2. bootloader env shows a rolled-back trial
       (`upgrade_available=0, bootcount!=0`, mirroring the agent's existing
       commit-protocol tuple logic) ⇒ **ota_rollback**
    3. dmesg/prior-boot shows watchdog reset (`bcm2835-wdt`/`watchdog`) ⇒ **watchdog**
    4. throttle bitmask "undervoltage occurred" and no clean marker ⇒ **undervoltage**
    5. clean-shutdown marker present ⇒ **clean**
    6. else ⇒ **unknown**
  - Clean-marker helpers: `mark_clean_shutdown(state_dir)` /
    `clean_marker_present(state_dir)`. The agent writes the marker on graceful
    SIGTERM (in the existing shutdown path in `agent.py`), so an absent marker on
    next boot means the shutdown was not clean.

- **`station_agent/power.py`** — `read_throttle() -> dict | None`. Runs
  `vcgencmd get_throttled` (feature-detect via `shutil.which`), parses the
  `throttled=0x…` bitmask into the boolean fields. Returns `None` if `vcgencmd`
  is absent (qemu) — caller omits the `power` block.

- **`station_agent/storage.py`** — `read_storage_health() -> dict | None`.
  - `root_block_device()` — resolves the backing `mmcblkX` of `/` (via
    `/sys` / `findmnt`-free sysfs walk; degrade to None).
  - For each mmc device: `kind` from `/sys/block/<dev>/device/type` or name
    heuristics; if `mmc` binary present, `mmc extcsd read /dev/<dev>` → parse
    `DEVICE_LIFE_TIME_EST_TYP_A/B` (hex 0x0A ⇒ 100%… mapped to percent bands) and
    `PRE_EOL_INFO` (0x01 normal / 0x02 warning / 0x03 urgent). SD ⇒ life/pre-eol
    `null`/`"n/a"`.
  - `io_error_count` — scan `dmesg` for `mmc`/`I/O error` lines (bounded; works
    for SD and eMMC; degrade to 0 if dmesg unreadable).

- **`station_agent/config.py`** — add `state_dir: str = "/var/lib/station-agent"`.
  Not required by `validate()` (degrades if unwritable).

The existing `bootloader.get_active_slot` and `inventory.get_current_version` are
reused for the `slot` block; `last_ota_result` is read from the agent's own view
of the last deployment if available, else `null` (authoritative OTA state stays
server-side — see below).

### Agent tests

All new logic is pure/parsing-heavy and tested with fixtures (tmp dirs, fake
`/sys/fs/pstore` trees, captured `vcgencmd`/`mmc extcsd` output strings,
synthetic dmesg). `compute_reboot_reason` gets a table of evidence→reason cases.
`detect_boot` gets new-boot / same-boot / unwritable-state-dir cases.

> **Real-HW honesty (CLAUDE.md serial-boundary spirit):** these collectors read
> sysfs/proc/`vcgencmd`/`mmc` that QEMU/native-sim cannot fully reproduce. Unit
> tests with fixtures verify the *parsing and decision logic* — the appropriate
> level here. Live signal correctness (real pstore record, real extcsd, real
> undervoltage) is validated once the PROVIDER image lands and on real CM4. The
> design’s graceful-degrade guarantees the agent is safe on sim/pre-merge
> regardless.

## Strang 2 — Server ingest & storage

### Serializer

`HeartbeatSerializer` gains `telemetry = serializers.DictField(required=False)`
(mirroring the existing optional `inventory`). No required fields inside — the
server validates leniently and stores what it gets.

### Model: `StationTelemetry` (new, `apps/stations`)

A `OneToOneField(Station)` snapshot model mirroring `StationInventory`'s pattern,
but with **extracted columns** for the fields alerting/UI query frequently, plus
the raw blob:

```python
class StationTelemetry(models.Model):
    station = OneToOneField(Station, related_name="telemetry", on_delete=CASCADE)
    data = JSONField(default=dict)                 # full telemetry blob (UI raw view)
    # boot
    boot_id = CharField(max_length=64, blank=True)
    boot_count = PositiveIntegerField(default=0)
    last_reboot_reason = CharField(max_length=16, blank=True)
    last_reboot_at = DateTimeField(null=True, blank=True)   # when server first saw new boot_id
    uptime_seconds = FloatField(null=True, blank=True)
    # power
    undervoltage_now = BooleanField(null=True)
    undervoltage_occurred = BooleanField(null=True)
    throttled_now = BooleanField(null=True)
    throttled_occurred = BooleanField(null=True)
    # slot / version / ota
    active_slot = CharField(max_length=1, blank=True)
    image_version = CharField(max_length=100, blank=True)
    last_ota_result = CharField(max_length=16, blank=True)
    # storage (summary; worst-case across devices for easy alerting)
    worst_life_time_pct = PositiveSmallIntegerField(null=True, blank=True)
    worst_pre_eol = CharField(max_length=8, blank=True)      # normal|warning|urgent|n/a|""
    io_error_count = PositiveIntegerField(default=0)
    updated_at = DateTimeField(auto_now=True)
```

Extracted columns keep the monitoring engine queries simple and indexable and
avoid repeated JSON digging; `data` preserves the full payload for the detail page.

### Ingest: `apps/stations/ingest.py` → `ingest_telemetry(station, telemetry: dict)`

Called from `HeartbeatView` after the existing station/inventory update, guarded
by `if telemetry:`. Responsibilities:

1. `update_or_create` the `StationTelemetry` row from the blob (defensive `.get`
   with graceful handling of missing sub-blocks).
2. **Reboot transition detection:** if incoming `boot_id` differs from the stored
   `boot_id` (and stored was non-empty), a reboot happened since the last
   heartbeat → set `last_reboot_at = now()`, write a `StationAuditLog` **REBOOT**
   event (new event type) carrying the reason, and store the new `boot_id`.
   First-ever heartbeat (stored empty) just records the boot_id without a REBOOT
   event (not a reboot we witnessed).
3. Keep it synchronous, small, `update_fields`-scoped — same philosophy as the
   current heartbeat handler.

OTA result stays authoritative on the existing `DeploymentResult` model; the
agent-reported `last_ota_result` is stored for display/cross-check only and does
**not** drive the OTA alert (avoids double-sourcing).

### Station detail UI

Add a **Telemetry** section/tab to `stations/station_detail.html` following the
existing inventory-tab markup:

- **Boot:** boot_count, uptime (humanized), last reboot reason (badge:
  green=clean, red=crash/watchdog, amber=ota_rollback/undervoltage, grey=unknown)
  + last_reboot_at.
- **Power:** undervoltage/throttle now/occurred chips (hidden if no `power` data).
- **Slot/Version:** active slot, image_version, last_ota_result.
- **Storage:** per-device table (kind, life A/B %, pre-eol badge, I/O errors);
  shows "n/a" for SD life/pre-eol.
- Degraded/missing blocks render as "not reported" rather than empty/broken.

Template guard: use `{% comment %}`, never multi-line `{# #}` (CI guard).

## Strang 3 — Alerting

Reuses `send_alert_notifications` + `recipients.py` (topology + `notify_channel`)
unchanged. Add new `AlertType`s and `engine.py` check functions following the
existing `_check_*` pattern (dedup via `_has_unresolved_alert`, auto-resolve
where meaningful).

### New alert types (`AlertType`)

- `UNEXPECTED_REBOOT = "unexpected_reboot"`
- `POWER_WARNING = "power_warning"`
- `STORAGE_HEALTH = "storage_health"`

(`STATION_OFFLINE` and `OTA_FAILED` already exist and already cover offline + OTA
fail/rollback — the existing `_check_ota_failed` includes `ROLLED_BACK`. We keep
them as-is; this feature adds the three missing dimensions.)

### New check functions (`engine.py`)

- **`_check_unexpected_reboot()`** — stations whose `telemetry.last_reboot_at` is
  within the check window with `last_reboot_reason ∈ {crash, watchdog,
  undervoltage, unknown}` (i.e. **not** `clean` and **not** `ota_rollback` — an
  OTA rollback is reported by the OTA alert, a clean reboot is expected). These
  are **event alerts** (do not auto-resolve; cleared by acknowledgement).
  **Dedup must be per-reboot, not per-station:** plain "unresolved alert exists"
  would permanently suppress a *second* genuine reboot, since event alerts never
  auto-resolve. Dedup key is therefore the reboot event itself — skip only if an
  `UNEXPECTED_REBOOT` alert already exists for the station with
  `created_at >= last_reboot_at` (i.e. already alerted for *this* reboot). A later
  distinct reboot (newer `last_reboot_at`) re-alerts. Message includes the reason
  and boot_count.

- **`_check_power_warning()`** — `undervoltage_occurred` or `throttled_occurred`
  true. Severity: `critical` if `*_now`, else `warning`. Auto-resolves when both
  `*_now` are false on a later heartbeat (occurred-bits persist until reboot, so
  resolution keys off the `now` bits).

- **`_check_storage_health()`** — `worst_pre_eol ∈ {warning, urgent}`, or
  `worst_life_time_pct ≥ rule.threshold` (default 80), or `io_error_count`
  increased beyond a stored baseline. Severity: `critical` for `urgent`/high
  wear, else `warning`. Wear alerts do not auto-resolve (wear is monotonic);
  I/O-error alerts dedup per station.

### Default AlertRules

`create_default_alert_rules.py` gains rows for the three new types (sensible
thresholds: storage life 80%, enabled, severity defaults). Idempotent
`get_or_create` as today.

### Alerting tests

Follow `tests/test_monitoring.py` patterns: construct `StationTelemetry` state,
run the check, assert alert creation, dedup (second run no dupe), auto-resolve
where applicable, and that `send_alert_notifications` routes via topology
(reuse `tests/test_notification_dispatch*.py` fixtures). Add an ingest test:
heartbeat with a changed `boot_id` creates a REBOOT audit event and populates
`StationTelemetry`; unchanged `boot_id` does not.

## Data flow (end-to-end)

1. Agent boots → `detect_boot` computes reboot_reason once, bumps boot_count.
2. Each heartbeat → `collect_telemetry` snapshots boot/power/slot/storage →
   POST `/api/v1/heartbeat/`.
3. `HeartbeatView` → `ingest_telemetry` upserts `StationTelemetry`, detects
   boot_id change → REBOOT audit event.
4. Periodic `manage.py check_alerts` → new `_check_*` raise `Alert`s →
   `send_alert_notifications` → topology-routed EMAIL/PUSH/Telegram.
5. Operator sees telemetry on the station detail page and alerts in the alert UI.

## Error handling & degradation

- **Agent:** every collector try/except → omit field, debug-log; heartbeat always
  sends. Unwritable `state_dir` → boot detection degrades (count 0, reason
  unknown) without raising.
- **Server:** `telemetry` optional; missing sub-blocks tolerated via `.get`;
  ingest wrapped so a malformed telemetry blob logs and is skipped without
  failing the heartbeat (heartbeat liveness must never depend on telemetry
  parsing).
- **Alerting:** unchanged channel isolation (one dead push endpoint / email
  failure never blocks others).

## Scope / YAGNI

- **No time-series store.** Snapshot (`StationTelemetry`) + audit events (reboot
  history) match the existing design and the brief; a metrics DB is explicitly
  out of scope.
- **OTA alerting not re-implemented** — reuse existing `_check_ota_failed`.
- **journald prior-boot tail** is read only as optional diagnostic evidence for
  reboot-reason; no log shipping.
- **One coherent PR** for the whole phase (coordinator default).

## Testing strategy summary

- Agent: unit tests for `compute_reboot_reason` (evidence table), `detect_boot`
  (new/same/unwritable), `power` bitmask parse, `storage` extcsd/dmesg parse,
  `collect_telemetry` graceful-degrade (all sources missing ⇒ no exception).
- Server: serializer accepts telemetry; `ingest_telemetry` upsert + reboot
  transition + audit event; detail page renders telemetry + degraded states.
- Monitoring: three new checks (raise/dedup/resolve) + topology routing + default
  rules seeding.
- `python -m pytest -q` with `config.settings.test`.
