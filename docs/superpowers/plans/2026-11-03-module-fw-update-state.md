# Module FW Update State Implementation Plan (station-manager)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the module firmware update state readable (one central status function → list, detail, station card, API), give every flash attempt a complete audit trail, stop after 5 transient failures with a "Erneut versuchen" retry, and detect a bootloader key mismatch before flashing.

**Architecture:** Persisted states (`failed`, `not_updatable`, `phase`) live on `ModuleFirmwareConvergenceState`. Derived waiting states are computed by one pure read function in `apps/module_firmware/update_status.py`. The reconciler owns state transitions and the key check, the reconcile API owns phase/audit/409s, and a new `mcuboot.py` owns the KEYHASH TLV parsing and the dev-key constant.

**Tech Stack:** Django 6.0, DRF 3.17 + drf-spectacular, HTMX, Alpine, pytest + pytest-django (+ django-test-migrations `migrator`), imgtool 2.5.0 (fixture generation only, not a runtime dependency).

**Spec:** `docs/superpowers/specs/2026-11-03-module-fw-update-state-design.md`

## Global Constraints

- Migrations are additive only. The data migration sets `updating` rows with `transient_failures >= 5` to `failed`, `reason_code=transient_limit`.
- `N = DRAIN_TRANSIENT_LIMIT = 5` transient failures **in a row**. The existing resets (at `verifying`, at a successful commit) stay.
- `failed` really stops: check hands out no instruction for `failed`, `not_updatable` or `quarantined` (user decision a). The existing test `test_check_still_offers_instruction_past_transient_cap` gets **inverted**, not deleted.
- Key check applies to the `dfu` artifact only. Synthetic (`uid_source=synthetic`, artifact `sim`) modules always skip it.
- Boot key = first **16 lowercase hex chars** of the SHA-256 KEYHASH TLV (type `0x01`). MCUboot dev key (`root-ec-p256.pem`) prefix: **`e30466f6b8470c1f`** (verified with imgtool 2.4.0 and 2.5.0 `dumpinfo`, equals `sha256(DER SubjectPublicKeyInfo)`).
- Audit only on state changes or real events, never per heartbeat or per check poll. "Wartet" states are never persisted or audited.
- German UI texts, copied **exactly** as written (Task 6 has the table).
- Templates: `{% comment %}…{% endcomment %}` only. Never use multi-line `{# #}` (the CI template guard enforces this).
- Visibility/permissions unchanged. Retry uses the same permission as quarantine release (`modules.manage`).
- Lock order stays module → assignment → convergence (see `reconcile_api_views.py` R2-1 comments).
- Audit writes are best-effort (`reconciler._audit` pattern): they never break the state change.
- Tests: `python -m pytest -q` (module_firmware tests live in `tests/module_firmware/`, API tests in `tests/test_api_module_firmware_*.py`, agent tests at `tests/test_*.py`, following the existing layout). Lint: `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .`.
- No FW release tag, no linux-image release, no SRCREV bump.

## Review Focus

1. **Rows over the transient limit that are still `updating` after deploy, before the data migration has touched them** (e.g. a row created at limit by a race). Expected: check never hands them out, and they show "Fehler". Task 4 adds a defensive `transient_failures >= DRAIN_TRANSIENT_LIMIT` → `failed` promotion inside `reconcile_module`, with test `test_reconcile_promotes_updating_row_at_limit_to_failed`.
2. **A module that reported a boot key once and later reports none** (re-flashed with old FW at the bench). Expected: `reported_boot_key` is cleared (it mirrors the current report), and a sticky `not_updatable` row stays until hashes match or a new target exists. Test `test_ingest_clears_boot_key_when_field_absent` (Task 3) and `test_not_updatable_stays_when_module_stops_reporting_key` (Task 4).
3. **Malformed or truncated `.signed.bin`, or a non-MCUboot blob, during import.** Expected: `boot_key_hash=""`, the import still succeeds, nothing raises. Tests `test_parse_truncated_returns_empty`, `test_parse_wrong_magic_returns_empty`, `test_parse_tlv_len_overflow_returns_empty` (Task 2).
4. **Garbage `boot_key` from FW** (uppercase, wrong length, non-hex, non-string). Expected: uppercase is lowercased; anything else is treated as absent and never triggers `not_updatable`. Test `test_ingest_rejects_malformed_boot_key` (Task 3).
5. **Repeated identical agent calls** (same phase POSTed 3×, check polled 10× for the same open instruction, the same 409 retried). Expected: exactly one STARTED/PHASE/INSTRUCTED entry, and one CONFLICT per rejected call. Tests in Task 5 (`test_same_phase_twice_logs_once`, `test_check_polling_logs_instructed_once`).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/module_firmware/models.py` (modify) | New choices/fields (`State.FAILED/NOT_UPDATABLE`, `Phase`, `ReasonCode`, `phase`, `phase_at`, `reason_code`, `instructed_at`, `Release.boot_key_hash`, `Module.reported_boot_key`, `Module.Convergence.FAILED/NOT_UPDATABLE`) |
| `apps/module_firmware/migrations/0017_update_state.py` / `0018_failed_transient_rows.py` (create) | Schema + data migration |
| `apps/stations/models.py` + `apps/stations/migrations/0030_module_flash_events.py` | 7 new `StationAuditLog.EventType` values |
| `apps/module_firmware/mcuboot.py` (create) | KEYHASH TLV parser, boot-key normalisation, dev-key constant, mismatch + hint predicates and texts |
| `apps/module_firmware/releases.py` (modify) | Store `boot_key_hash` on import |
| `apps/module_firmware/management/commands/backfill_release_boot_keys.py` (create) | Idempotent backfill from storage |
| `apps/module_firmware/ingest.py` (modify) | `identity.boot_key` → `Module.reported_boot_key` |
| `apps/module_firmware/reconciler.py` (modify) | Sticky states, key check, transient→failed, hint text, pure target resolver |
| `apps/module_firmware/reconcile_api_views.py` (modify) | INSTRUCTED dedup, phase tracking/audit, 409 + CONFLICT |
| `apps/module_firmware/update_status.py` (create) | `UpdateStatus`, `module_update_status`, `bulk_update_status` |
| `apps/module_firmware/services.py` + `views_convergence.py` + `urls.py` (modify) | `retry_failed` service + GUI POST view |
| `apps/api/module_firmware_serializers.py` / `module_firmware_views.py` (modify) | `update_status`, `reported_boot_key`, `boot_key_hash`, `retry-update` action |
| templates (modify/create) | `_update_status_pill.html`, list column, detail, station card, convergence pill |
| `tests/fixtures/mcuboot/` (create) | imgtool-signed dev-key and other-key binaries + generator script |

---

### Task 1: Models, migrations, audit event types

**Files:**
- Modify: `apps/module_firmware/models.py` (`Module.Convergence`, `ModuleFirmwareRelease`, `ModuleFirmwareConvergenceState`, comment at lines 6-12 for `DRAIN_TRANSIENT_LIMIT`)
- Modify: `apps/stations/models.py:375-414` (`StationAuditLog.EventType`)
- Create: `apps/module_firmware/migrations/0017_update_state.py`, `0018_failed_transient_rows.py`, `apps/stations/migrations/0030_module_flash_events.py` (generate with `python manage.py makemigrations`; write 0018 by hand)
- Test: `tests/module_firmware/test_update_state_models.py`, `tests/module_firmware/test_update_state_migration.py`

**Interfaces:**
- Produces:
  - `ModuleFirmwareConvergenceState.State.FAILED = "failed"`, `.NOT_UPDATABLE = "not_updatable"`
  - `ModuleFirmwareConvergenceState.Phase` (TextChoices): `DOWNLOADING="downloading"`, `FLASHING="flashing"`, `VERIFYING="verifying"`
  - `ModuleFirmwareConvergenceState.ReasonCode` (TextChoices): `TRANSIENT_LIMIT="transient_limit"`, `BOOT_KEY_MISMATCH="boot_key_mismatch"`
  - fields `phase` (CharField 16, choices Phase, blank, default ""), `phase_at` (DateTimeField null), `reason_code` (CharField 32, choices ReasonCode, blank, default ""), `instructed_at` (DateTimeField null; set when an instruction is audited, cleared by a terminal status, commit and retry)
  - `Module.Convergence.FAILED = "failed"`, `.NOT_UPDATABLE = "not_updatable"`; `Module.reported_boot_key` (CharField 16, blank, default "")
  - `ModuleFirmwareRelease.boot_key_hash` (CharField 16, blank, default "")
  - `StationAuditLog.EventType`: `MODULE_FLASH_INSTRUCTED="module_flash_instructed"`, `MODULE_FLASH_PHASE="module_flash_phase"`, `MODULE_FLASH_CONFLICT="module_flash_conflict"`, `MODULE_FLASH_GAVE_UP="module_flash_gave_up"`, `MODULE_NOT_UPDATABLE="module_not_updatable"`, `MODULE_UPDATABLE_AGAIN="module_updatable_again"`, `MODULE_FLASH_RETRY="module_flash_retry"` (labels in the existing English style, e.g. `_("Module Flash Instructed")`; all ≤ 32 chars)

- [ ] **Step 1: Write failing tests**

```python
# tests/module_firmware/test_update_state_models.py
def test_new_convergence_choices():
    S = ModuleFirmwareConvergenceState.State
    assert {S.FAILED, S.NOT_UPDATABLE} <= set(S.values)
    assert {"failed", "not_updatable"} <= set(Module.Convergence.values)
    assert set(ModuleFirmwareConvergenceState.Phase.values) == {"downloading", "flashing", "verifying"}

def test_new_fields_default_blank(fm):  # create module/release/row with defaults
    assert (cs.phase, cs.phase_at, cs.reason_code, cs.instructed_at) == ("", None, "", None)
    assert m.reported_boot_key == "" and rel.boot_key_hash == ""

def test_new_audit_event_types_fit_column():
    for name in ["MODULE_FLASH_INSTRUCTED", "MODULE_FLASH_PHASE", "MODULE_FLASH_CONFLICT",
                 "MODULE_FLASH_GAVE_UP", "MODULE_NOT_UPDATABLE", "MODULE_UPDATABLE_AGAIN",
                 "MODULE_FLASH_RETRY"]:
        assert len(getattr(StationAuditLog.EventType, name).value) <= 32
```

```python
# tests/module_firmware/test_update_state_migration.py  (django_test_migrations `migrator`, pattern: tests/test_rollout_groups_migration.py)
PRE = ("module_firmware", "0016_alter_modulefirmwaretarget_canary_group_and_more")
TESTED = ("module_firmware", "0018_failed_transient_rows")

def test_rows_at_limit_become_failed(migrator):
    # seed: row A updating tf=5, row B updating tf=4, row C quarantined tf=7; module of A rollup "updating"
    # after: A.state == "failed", A.reason_code == "transient_limit"; B stays updating; C stays quarantined
    # module of A: firmware_convergence == "failed"
```

- [ ] **Step 2: Run, expect FAIL** — `python -m pytest -q tests/module_firmware/test_update_state_models.py tests/module_firmware/test_update_state_migration.py` → AttributeError / migration not found.

- [ ] **Step 3: Implement** the model fields/choices, then `python manage.py makemigrations module_firmware stations`, and rename the generated files to the names above. Hand-write `0018` as `RunPython(forward, migrations.RunPython.noop)`. `forward` filters `state="updating", transient_failures__gte=5` (hardcode 5, do not import the live constant) and bulk-updates `state="failed", reason_code="transient_limit"`. It then sets `firmware_convergence="failed"` on the affected modules whose rollup is `"updating"`. Update the `DRAIN_TRANSIENT_LIMIT` comment: at the limit the row becomes `failed`, check stops, and only retry or a new target continues.

- [ ] **Step 4: Run, expect PASS.** Also run `python manage.py makemigrations --check --dry-run` (expect "No changes detected").

- [ ] **Step 5: Commit** — `git commit -m "feat(module-fw): update-state fields, failed/not_updatable states, flash audit events"`

---

### Task 2: KEYHASH parser, dev-key fixture, release import, backfill

**Files:**
- Create: `apps/module_firmware/mcuboot.py`
- Create: `tests/fixtures/mcuboot/make_fixtures.sh`, `tests/fixtures/mcuboot/dev-signed.bin`, `tests/fixtures/mcuboot/other-signed.bin`, `tests/fixtures/mcuboot/other-key.pem` (throwaway test key, never used anywhere else)
- Modify: `apps/module_firmware/releases.py` (`_StagedVariant` + `update_or_create` defaults)
- Create: `apps/module_firmware/management/__init__.py`, `management/commands/__init__.py`, `management/commands/backfill_release_boot_keys.py`
- Test: `tests/module_firmware/test_mcuboot_keyhash.py`, extend `tests/module_firmware/test_import_release.py`

**Interfaces:**
- Produces (in `apps/module_firmware/mcuboot.py`):
  - `BOOT_KEY_PREFIX_LEN = 16`
  - `DEV_KEY_HASH_PREFIX = "e30466f6b8470c1f"`
  - `parse_boot_key_hash(blob: bytes) -> str`: 16-hex prefix of the KEYHASH TLV, `""` when absent or malformed. **Never raises.**
  - `normalize_boot_key(value: object) -> str`: lowercases; returns `""` unless the result matches `^[0-9a-f]{16}$`
  - `is_dev_key(prefix: str) -> bool`
  - (Task 4 adds the predicates/texts to this module)

- [ ] **Step 1: Generate fixtures.** `make_fixtures.sh` is checked in and is the only source of the binaries:

```bash
#!/usr/bin/env bash
# Regenerates the MCUboot KEYHASH fixtures. Dev key = MCUboot's public root-ec-p256.pem
# (https://raw.githubusercontent.com/mcu-tools/mcuboot/main/root-ec-p256.pem,
#  sha256 1734d7ee76344fbf2b1731f49baa5e1ced9bea56b66fe84703b1a492e0a13614).
set -euo pipefail
cd "$(dirname "$0")"
head -c 2048 /dev/zero | tr '\0' '\125' > /tmp/payload.bin
IMG="uvx --from imgtool==2.5.0 imgtool"
$IMG sign --key root-ec-p256.pem --header-size 0x200 --pad-header --align 4 \
  --version 1.2.3 --slot-size 0x20000 /tmp/payload.bin dev-signed.bin
$IMG sign --key other-key.pem   --header-size 0x200 --pad-header --align 4 \
  --version 1.2.3 --slot-size 0x20000 /tmp/payload.bin other-signed.bin
```

Download `root-ec-p256.pem` into the fixture dir (check the sha256 above), and create `other-key.pem` once with `imgtool keygen -k other-key.pem -t ecdsa-p256`. Run the script. Run `imgtool dumpinfo dev-signed.bin` and confirm KEYHASH starts `0xe3 0x04 0x66 0xf6 0xb8 0x47 0x0c 0x1f`. Commit the `.bin` files (about 2.7 KB each), both PEMs and the script.

- [ ] **Step 2: Write failing tests**

```python
# tests/module_firmware/test_mcuboot_keyhash.py
FIX = Path(__file__).parent.parent / "fixtures" / "mcuboot"

def test_dev_signed_fixture_yields_dev_prefix():
    assert parse_boot_key_hash((FIX / "dev-signed.bin").read_bytes()) == DEV_KEY_HASH_PREFIX

def test_dev_prefix_matches_dev_pem():  # guards the constant
    # sha256(DER SubjectPublicKeyInfo of root-ec-p256.pem)[:16] via `cryptography`
    assert _spki_hash(FIX / "root-ec-p256.pem")[:16] == DEV_KEY_HASH_PREFIX

def test_other_key_differs_from_dev():
    h = parse_boot_key_hash((FIX / "other-signed.bin").read_bytes())
    assert len(h) == 16 and h != DEV_KEY_HASH_PREFIX
    assert h == _spki_hash(FIX / "other-key.pem")[:16]

def test_parse_truncated_returns_empty():      # blob[:600], blob[:10], b""
def test_parse_wrong_magic_returns_empty():    # first 4 bytes zeroed
def test_parse_tlv_len_overflow_returns_empty():  # TLV info total len patched to 0xFFFF
def test_parse_without_keyhash_returns_empty():  # KEYHASH entry type patched to 0x7f

@pytest.mark.parametrize("raw,expected", [("E30466F6B8470C1F", "e30466f6b8470c1f"),
    ("e30466f6", ""), ("zz0466f6b8470c1f", ""), (None, ""), (123, ""), ("", "")])
def test_normalize_boot_key(raw, expected): ...
```

```python
# tests/module_firmware/test_import_release.py (add)
def test_import_stores_boot_key_hash(fm):      # _fake_download serves dev-signed.bin bytes for *.signed.bin
    assert rel.boot_key_hash == DEV_KEY_HASH_PREFIX
def test_import_non_mcuboot_blob_leaves_hash_empty(fm):  # existing b"signed-vhf" blobs
    assert job.status == READY and rel.boot_key_hash == ""
```

```python
# tests/module_firmware/test_backfill_boot_keys.py
def test_backfill_fills_empty_only(fm, tmp_storage):  # release A empty + dev blob in storage, B pre-set "ffff…"
    call_command("backfill_release_boot_keys")
    # A == DEV_KEY_HASH_PREFIX, B unchanged; second run changes nothing
def test_backfill_missing_object_is_skipped(fm):  # storage key absent → no crash, stays ""
```

- [ ] **Step 3: Run, expect FAIL** (ImportError).

- [ ] **Step 4: Implement `parse_boot_key_hash`.** Image layout (little-endian): header `magic u32 @0 == 0x96F3B83D`, `hdr_size u16 @8`, `img_size u32 @12`. TLV area starts at `hdr_size + img_size`. An optional protected info (`u16 magic 0x6908, u16 total_len`) is skipped by `total_len`. Then the unprotected info (`0x6907, total_len` incl. the 4-byte info). Entries are `u16 type, u16 len, data`. Search **both** areas for type `0x01` with `len == 32` and return `data.hex()[:16]`. Bounds-check every read with `struct.unpack_from` inside `try/except struct.error` → `""`.

- [ ] **Step 5: Wire into import.** Add `boot_key_hash: str` to `_StagedVariant`, set it from `parse_boot_key_hash(blob)` after the sha/cosign verification, and add `"boot_key_hash": sv.boot_key_hash` to the `update_or_create` defaults (so a restore re-parses). The sim artifact gets nothing.

- [ ] **Step 6: Backfill command.** `backfill_release_boot_keys` iterates `ModuleFirmwareRelease.all_objects.filter(boot_key_hash="").exclude(storage_key="")`, reads via `storage.open_stream`, parses, and saves non-empty results with `update_fields=["boot_key_hash"]`. A missing storage object → `self.stderr` line, continue. Prints `"updated N of M"`. No data migration: storage access stays out of migrations.

- [ ] **Step 7: Run, expect PASS.** `python -m pytest -q tests/module_firmware/test_mcuboot_keyhash.py tests/module_firmware/test_import_release.py tests/module_firmware/test_backfill_boot_keys.py`

- [ ] **Step 8: Commit** — `feat(module-fw): parse MCUboot KEYHASH on release import (+ backfill command)`

---

### Task 3: Ingest `identity.boot_key` + agent pass-through

**Files:**
- Modify: `apps/module_firmware/ingest.py` (`ingest_module`: create defaults + update branch)
- Test: `tests/module_firmware/test_ingest_boot_key.py`, extend `tests/test_slot_discovery.py`, extend `tests/module_firmware/test_apply_inventory_e2e.py`

**Interfaces:**
- Consumes: `mcuboot.normalize_boot_key`
- Produces: `Module.reported_boot_key` mirrors the latest report. Present+valid → stored. Absent or invalid → `""`. Saved with `update_fields` only when changed.

- [ ] **Step 1: Write failing tests**

```python
def test_ingest_stores_boot_key(station, fm):          # identity boot_key="E30466F6B8470C1F" → "e30466f6b8470c1f" (create path)
def test_ingest_updates_boot_key_on_existing(station, fm):  # second ingest with other key overwrites
def test_ingest_clears_boot_key_when_field_absent(station, fm):  # stored → identity w/o boot_key → ""
def test_ingest_rejects_malformed_boot_key(station, fm):  # "xyz" → "", no audit row written
def test_ingest_boot_key_no_audit_spam(station, fm):  # 5 identical ingests → StationAuditLog count unchanged vs 1 ingest
```

```python
# tests/test_slot_discovery.py
def test_probe_slot_passes_identity_boot_key_through(tmp_path):
    # fake peer returns MODULE-DESCRIBE with identity.boot_key="e30466f6b8470c1f"
    assert modules[0]["identity"]["boot_key"] == "e30466f6b8470c1f"
```

```python
# tests/module_firmware/test_apply_inventory_e2e.py
def test_inventory_boot_key_reaches_module(fm, station_factory):
    # apply_inventory(...) with identity incl. boot_key → Module.reported_boot_key set
```

- [ ] **Step 2: Run, expect FAIL** (the slot_discovery test is expected to PASS already, since `identity` is passed whole at `slot_discovery.py:180` and `broker.py:125/293/315`. Keep it as a regression pin).
- [ ] **Step 3: Implement.** `boot_key = normalize_boot_key(identity.get("boot_key"))`. Add it to `get_or_create` defaults. In the update branch, include `reported_boot_key` in the existing `module.save(update_fields=[…])` call. Do this before `reconcile_module(module)` so the key check sees the fresh value. Log (no audit) at WARNING once per ingest when a non-empty raw value failed normalisation.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(module-fw): ingest reported bootloader key from module identity`

---

### Task 4: Reconciler — sticky states, key check, transient → failed, hint text

**Files:**
- Modify: `apps/module_firmware/reconciler.py` (`effective_target`, `reconcile_module`, `record_error`)
- Modify: `apps/module_firmware/mcuboot.py` (predicates + texts)
- Modify: `apps/module_firmware/drain.py` (docstrings only: `pending_updates` / `draining_stations`. The transient filter stays as a harmless guard)
- Test: `tests/module_firmware/test_reconciler_update_state.py`; extend `tests/module_firmware/test_reconcile_audit.py`

**Interfaces:**
- Consumes: Task 1 fields, `mcuboot.DEV_KEY_HASH_PREFIX`
- Produces:
  - `resolve_effective_target(station, targets: Iterable[ModuleFirmwareTarget]) -> ModuleFirmwareTarget | None`: pure; same precedence and canary rule as today. `effective_target(station, module_type)` becomes one query (`ModuleFirmwareTarget.objects.filter(module_type=module_type)`) + this resolver. Task 6 uses the resolver for bulk.
  - in `mcuboot.py`:
    - `boot_key_mismatch(module, release) -> bool`: False if `module.uid_source == synthetic` or either key empty, else `module.reported_boot_key != release.boot_key_hash`
    - `not_updatable_text(module) -> str`: the two exact spec texts (Dev-Key / anderer Schlüssel)
    - `KEY_UNKNOWN_HINT = "Mögliche Ursache: Bootloader-Schlüssel passt nicht zur Release (z. B. Dev-Key) – Modul-Firmware zu alt, um das zu prüfen."`
    - `key_hint_applies(module, release, error_mode: str) -> bool`: `error_mode == "rolled_back"`, module not synthetic, and (`reported_boot_key == ""` or `release.boot_key_hash == ""`)
  - `reconcile_module(module)`: signature/return unchanged
  - `record_error(convergence, error_mode, error_message="")`: signature unchanged; may now set FAILED

Exact texts (copy verbatim):
- Dev: `Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release (Dev-Key) – SWD-Reflash an der Bench nötig`
- Other: `Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release (anderer Schlüssel) – SWD-Reflash an der Bench nötig`

Decision order in `reconcile_module` after `desired` is resolved and the row is locked (replaces the current QUARANTINED early-return + version branch):

1. `reported == desired.version` and `state != QUARANTINED` → OK; clear `transient_failures`, `reason_code`, `phase`, `phase_at`, `instructed_at`. Rollup OK. (Quarantine keeps today's behaviour.)
2. `boot_key_mismatch(module, desired)` → if `state != NOT_UPDATABLE`: set `state=NOT_UPDATABLE`, `reason_code=boot_key_mismatch`, clear phase, audit `MODULE_NOT_UPDATABLE` with `not_updatable_text(module)`. This overrides `updating`, `failed` and `quarantined`. Rollup NOT_UPDATABLE. Return.
3. `state == NOT_UPDATABLE` and both keys known and equal → `state=UPDATING`, `attempts=0`, `transient_failures=0`, `reason_code=""`, audit `MODULE_UPDATABLE_AGAIN`. Fall through to step 6.
4. `state in (QUARANTINED, NOT_UPDATABLE, FAILED)` → sticky; set the matching rollup and return the row.
5. `state == UPDATING and transient_failures >= DRAIN_TRANSIENT_LIMIT` → promote to FAILED (`reason_code=transient_limit`), audit GAVE_UP (Review Focus 1).
6. Otherwise UPDATING (as today).

`record_error` changes:
- All modes clear `phase`, `phase_at` and `instructed_at`.
- transient: after `transient_failures += 1`, if `>= DRAIN_TRANSIENT_LIMIT` → `state=FAILED`, `reason_code=transient_limit`, rollup FAILED, audit `MODULE_FLASH_GAVE_UP`: `f"Modul {uid}: Update nach {n} Fehlversuchen in Folge abgebrochen (Ziel {version}). Letzte Meldung: {msg}"`.
- The `MODULE_QUARANTINED` message gets ` Mögliche Ursache: …` (`KEY_UNKNOWN_HINT`) appended when `key_hint_applies(module, release, error_mode)`.
- The rollup else-branch must not overwrite FAILED with UPDATING.

- [ ] **Step 1: Write failing tests** (`tests/module_firmware/test_reconciler_update_state.py`, fixtures as in `test_reconcile_status.py::_setup`, release with `boot_key_hash`)

```python
def test_resolve_effective_target_matches_effective_target(...)  # station > group > fleet, canary excluded → None
def test_effective_target_single_query(django_assert_num_queries)  # == 1
def test_transient_limit_sets_failed_and_audits_once(fm, station)  # 5× record_error(TRANSIENT) → FAILED, reason transient_limit, rollup failed, 1 GAVE_UP
def test_failed_keeps_last_error_message(fm, station)
def test_four_transients_then_verifying_reset_never_fails(...)  # via record_error ×4 + reset to 0 + ×4 → still UPDATING
def test_reconcile_promotes_updating_row_at_limit_to_failed(fm, station)  # row created tf=5 updating
def test_failed_is_sticky_across_reconcile(fm, station)
def test_failed_becomes_ok_when_version_reached(fm, station)
def test_dev_key_mismatch_sets_not_updatable_with_dev_text(fm, station)  # module key = DEV prefix, release "1111…"
    # state NOT_UPDATABLE, reason boot_key_mismatch, rollup not_updatable, audit message == exact Dev text
def test_other_key_mismatch_text(fm, station)   # module "2222…" → "(anderer Schlüssel)"
def test_mismatch_audited_once_over_many_reconciles(fm, station)  # 20× reconcile_module → 1 MODULE_NOT_UPDATABLE
def test_mismatch_overrides_failed_and_quarantined(fm, station)  # parametrize start state
def test_keys_equal_again_releases_only_not_updatable(fm, station)  # → UPDATING, attempts=0, tf=0, 1 UPDATABLE_AGAIN
def test_keys_equal_does_not_release_quarantine_or_failed(fm, station)
def test_not_updatable_stays_when_module_stops_reporting_key(fm, station)
def test_synthetic_module_skips_key_check(fm, station)  # uid_source synthetic, differing keys → UPDATING
def test_missing_key_on_either_side_keeps_old_behaviour(fm, station)  # parametrize: module "" / release ""
def test_quarantine_after_rolled_back_with_unknown_key_has_hint(fm, station)  # 3× ROLLED_BACK → message contains KEY_UNKNOWN_HINT
def test_quarantine_after_rejected_has_no_hint / test_gave_up_has_no_hint(fm, station)
def test_record_error_clears_phase_and_instructed_at(fm, station)
```

Invert `tests/module_firmware/test_reconcile_check_gating.py::test_check_still_offers_instruction_past_transient_cap` → rename to `test_check_stops_past_transient_cap`: expect 204, and the row is FAILED after the check.

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** per the decision order above. Use the existing `_audit(module, event, message, station=...)`. For the station, pass the open-assignment station when it is already at hand (`assignment.station` in `reconcile_module`).
- [ ] **Step 4: Run** `python -m pytest -q tests/module_firmware/` → PASS (whole dir: existing drain/check/commit tests must stay green).
- [ ] **Step 5: Commit** — `feat(module-fw): failed/not_updatable states, boot key check in reconciler`

---

### Task 5: Reconcile API — INSTRUCTED dedup, phases, 409 + CONFLICT

**Files:**
- Modify: `apps/module_firmware/reconcile_api_views.py`
- Test: `tests/module_firmware/test_reconcile_lifecycle_audit.py`; extend `test_reconcile_status.py`, `test_reconcile_commit.py`, `test_reconcile_check.py`

**Interfaces:**
- Consumes: Task 1/4 (`instructed_at`, `phase`, `record_error` clears phase, `key_hint_applies`)
- Produces (agent-visible contract unchanged except new 409 cases):
  - **check:** 204 as today, plus never for FAILED/NOT_UPDATABLE/QUARANTINED. For the chosen instruction only: `rows = ModuleFirmwareConvergenceState.objects.filter(pk=cs.pk, instructed_at__isnull=True).update(instructed_at=now, phase="", phase_at=None)`. If `rows == 1`, audit `MODULE_FLASH_INSTRUCTED`: `f"Modul {uid}: Update-Anweisung auf {version} ausgegeben ({artifact})."` with `station`.
  - **status:** 409 for `state in (QUARANTINED, FAILED, NOT_UPDATABLE)`, with `MODULE_FLASH_CONFLICT` audit `f"Modul {uid}: Status '{status}' abgelehnt – Zustand {state}."`. OK keeps a 409 without audit (late callback, existing behaviour). Progress: if `new_status != cs.phase` → set `phase`, `phase_at=now`; audit `MODULE_FLASH_STARTED` when the old phase was `""` (attempt start, keep stamping `last_attempt_at` when None), else `MODULE_FLASH_PHASE` (`f"Modul {uid}: Phase {old} → {new}."`). Same phase again → no write, no audit. Outcome statuses: unchanged, except the rolled_back audit message gets `KEY_UNKNOWN_HINT` when `key_hint_applies` (record_error already clears phase and instructed_at). The existing DFU-stuck clear and the verifying transient reset stay.
  - **commit:** 409 + CONFLICT audit for QUARANTINED, FAILED, NOT_UPDATABLE (one entry per rejected call). On the success path, clear `phase`, `phase_at`, `instructed_at` before `reconcile_module`. The mismatch path appends the hint like status.

- [ ] **Step 1: Write failing tests**

```python
# test_reconcile_lifecycle_audit.py
def test_check_polling_logs_instructed_once(client, station_with_key, fm):  # 10× check → 1 INSTRUCTED
def test_instructed_logged_again_after_failed_status(...)  # check, status failed, check → 2 INSTRUCTED
def test_progress_status_does_not_reset_instructed(...)  # check, downloading, check → 1 INSTRUCTED
def test_phases_logged_once_each(...)  # downloading, downloading, flashing, verifying, verifying
    # → STARTED×1, PHASE×2; cs.phase == "verifying", phase_at set
def test_same_phase_twice_logs_once(...)
def test_commit_clears_phase(...)
def test_failed_status_clears_phase(...)
def test_new_instruction_clears_stale_phase(...)  # phase="flashing", instructed_at=None → check → phase == ""
def test_second_attempt_logs_started_again(...)  # downloading, failed, check, downloading → STARTED×2

# test_reconcile_status.py
@pytest.mark.parametrize("state", ["failed", "not_updatable", "quarantined"])
def test_status_409_on_given_up_row_logs_conflict(...)  # 409, row unchanged, 1 CONFLICT per POST (2 POSTs → 2)
def test_status_409_on_ok_row_not_audited(...)

# test_reconcile_commit.py
@pytest.mark.parametrize("state", ["failed", "not_updatable", "quarantined"])
def test_commit_409_on_given_up_row_logs_conflict(...)

# test_reconcile_check.py
@pytest.mark.parametrize("state", ["failed", "not_updatable"])
def test_check_skips_given_up_rows(...)  # 204
def test_check_204_on_dev_key_mismatch_and_no_drain(...)  # module DEV key, release other → 204, drain.pending_updates(station) == []
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** per the Interfaces block. Keep the existing lock order and the `transaction.atomic` blocks. CONFLICT audits go inside the atomic block via `_audit` (best-effort, already swallows errors).
- [ ] **Step 4: Run** `python -m pytest -q tests/module_firmware/` → PASS.
- [ ] **Step 5: Commit** — `feat(module-fw): lifecycle audit for instruct/phase/conflict in reconcile API`

---

### Task 6: Central `module_update_status` (N+1-free)

**Files:**
- Create: `apps/module_firmware/update_status.py`
- Test: `tests/module_firmware/test_update_status.py`

**Interfaces:**
- Consumes: `resolve_effective_target`, `drain.executable_artifact`, `drain.artifact_for`, `mcuboot.not_updatable_text`, `mcuboot.key_hint_applies`, `mcuboot.KEY_UNKNOWN_HINT`, `ControlLock`
- Produces:

```python
class UpdateState(models.TextChoices):   # stable API enum
    NO_TARGET = "no_target"; CURRENT = "current"; QUARANTINED = "quarantined"
    NOT_UPDATABLE = "not_updatable"; FAILED = "failed"; WAITING = "waiting"
    RUNNING = "running"; PENDING = "pending"

class WaitReason(models.TextChoices):
    AGENT_NO_MODULE_OTA = "agent_no_module_ota"; NO_RELEASE_FOR_VARIANT = "no_release_for_variant"
    NO_ARTIFACT = "no_artifact"; STATION_OFFLINE = "station_offline"; STATION_BUSY = "station_busy"

# reason_code enum for the API = WaitReason ∪ ConvergenceState.ReasonCode ∪ ErrorMode ("rejected", "rolled_back")
REASON_CODES: tuple[str, ...]

@dataclass(frozen=True)
class UpdateStatus:
    state: str            # UpdateState
    phase: str            # "" or Phase value
    reason_code: str      # "" or one of REASON_CODES
    reason_text: str      # full German display line (table below)
    since: datetime | None
    @property
    def is_live(self) -> bool: ...   # state in {running, waiting, pending} → UI keeps polling
    @property
    def short_label(self) -> str: ... # pill text: "Kein Ziel","Aktuell","Quarantäne","Nicht updatebar","Fehler","Wartet","Update läuft","Update ausstehend"

def module_update_status(module, *, station=None) -> UpdateStatus      # == bulk_update_status([module], ...)[module.pk]
def bulk_update_status(modules: Sequence[Module]) -> dict[int, UpdateStatus]
```

Priority (first match wins) and exact `reason_text`:

| # | state | condition | reason_code | reason_text | since |
|---|---|---|---|---|---|
| 1 | no_target | no open assignment w/ station, or no effective target | "" | `Kein Ziel` | None |
| 2 | current | `last_reported_version == target.version` | "" | `Aktuell` | None |
| 3 | quarantined | current row QUARANTINED | last_error_mode | `Quarantäne (Update {attempts}× zurückgerollt)` / `Quarantäne (vom Modul abgelehnt)`. When `key_hint_applies`: `Quarantäne (Update {attempts}× zurückgerollt – {KEY_UNKNOWN_HINT})` | row.updated_at |
| 4 | not_updatable | row NOT_UPDATABLE | boot_key_mismatch | `not_updatable_text(module)` | row.updated_at |
| 5 | failed | row FAILED | transient_limit | `Fehler: {last_error_message}`, or `Fehler: Update mehrfach fehlgeschlagen` when empty | row.updated_at |
| 6 | waiting | station not online (`last_seen` < 120 s, same as `Station.is_online`) | station_offline | `Wartet: Station offline` | None |
| 7 | running | row.phase set | "" | `Update läuft: Download` / `Update läuft: Flashen` / `Update läuft: Prüfen` | row.phase_at |
| 8a | waiting | `artifact_for(module)` ∉ `station.module_ota_backends()` | agent_no_module_ota | `Wartet: Station-Agent kann keine Module flashen (Image zu alt)` | None |
| 8b | waiting | no release for (type, variant, target.version) | no_release_for_variant | `Wartet: Keine Firmware {version} für Variante {variant or "–"} vorhanden` | None |
| 8c | waiting | release exists, `executable_artifact(...)` is None | no_artifact | `Wartet: Release enthält kein passendes Artefakt ({dfu\|sim})` | None |
| 8d | waiting | ControlLock with holder on station | station_busy | `Wartet: Station belegt (Bedienung aktiv)` | None |
| 9 | pending | otherwise | "" | `Update ausstehend` | row.created_at if row else None |

"Current row" = the convergence row for `(module, desired release)`. Without a release there is no row, so 3–5/7 are skipped. Pure read: no `reconcile_module`, no locks, no writes.

`bulk_update_status` query budget (independent of `len(modules)`): open assignments + stations (1, or use the `open_assignments` prefetch when present), all targets of the involved module types (1), matching releases (1), convergence rows of the modules for those releases (1), busy station ids (1).

- [ ] **Step 1: Write failing tests** — one per table row, using the exact strings:

```python
def test_no_target(...): assert s.state == "no_target" and s.reason_text == "Kein Ziel"
def test_current(...)
def test_quarantined_rolled_back_text(...): assert s.reason_text == "Quarantäne (Update 3× zurückgerollt)"
def test_quarantined_with_key_hint(...): assert KEY_UNKNOWN_HINT in s.reason_text
def test_not_updatable_dev_text(...)
def test_failed_shows_last_message(...): assert s.reason_text == "Fehler: dfu-util timeout"
def test_offline_beats_running_phase(...)  # phase="flashing", station.last_seen 10 min ago → waiting/station_offline
def test_running_phase_text(...): assert s.reason_text == "Update läuft: Flashen" and s.since == cs.phase_at
def test_old_agent_waits(...): # agent_capabilities={} → "Wartet: Station-Agent kann keine Module flashen (Image zu alt)"
def test_no_release_for_variant_text(...): assert s.reason_text == "Wartet: Keine Firmware 26.09.15-01 für Variante vhf vorhanden"
def test_no_artifact_sim(...): # synthetic module, backends ["sim"], release w/o sim → "Wartet: Release enthält kein passendes Artefakt (sim)"
def test_busy_station(...): # ControlLock(holder=user) → station_busy
def test_pending(...): assert s.state == "pending" and s.reason_text == "Update ausstehend"
def test_quarantine_beats_offline(...)  # priority 3 before 6
def test_is_live_flags(...)
def test_bulk_query_count_constant(django_assert_num_queries, ...):
    # 1 module vs 8 modules on 3 stations (mixed states): captured query counts equal, and ≤ 5
def test_bulk_matches_single(...)  # bulk_update_status(ms)[m.pk] == module_update_status(m) for all
def test_status_is_read_only(...)  # no new StationAuditLog, row.updated_at unchanged
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `module_update_status(module, station=...)` uses `station` instead of the assignment's station when given (the station card already has it).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(module-fw): central module_update_status (bulk, N+1-free)`

---

### Task 7: "Erneut versuchen" — service, GUI view, API action

**Files:**
- Modify: `apps/module_firmware/services.py` (add `retry_failed`)
- Modify: `apps/module_firmware/views_convergence.py` (add `ModuleRetryUpdateView`), `apps/module_firmware/urls.py`
- Modify: `apps/api/module_firmware_views.py` (`ModuleViewSet.retry_update`)
- Test: `tests/module_firmware/test_retry_failed.py`, extend `tests/test_api_module_firmware_modules.py`

**Interfaces:**
- Produces:
  - `services.retry_failed(module, *, user, ip_address=None, audit_suffix="") -> list[str]`: `@transaction.atomic`. Locks the module, runs `cs = reconcile_module(locked)` (current row), and acts only if `cs` is FAILED. It then resets `state=UPDATING`, `transient_failures=0`, `phase=""`, `phase_at=None`, `reason_code=""`, `instructed_at=None` (keeps `attempts`, `last_error_*`), calls `reconcile_module` again, and nudges + `refresh_lock_status` like `release_quarantine`. Audit `MODULE_FLASH_RETRY` with user/ip: `f"Update für Modul {uid} wird erneut versucht: {version}.{audit_suffix}"`, `changes={"retried_targets": [version]}`. Returns `[version]`, or `[]` (no-op, no audit) for any other state, including NOT_UPDATABLE and QUARANTINED.
  - URL `module_firmware:module_retry_update` → `POST /modules/<uid>/retry-update/`, `AreaPermissionMixin`, `area="modules"`, `verb="manage"`. Messages: success `"Update für %(uid)s wird erneut versucht (%(versions)s)."`, no-op `"%(uid)s ist nicht im Fehlerzustand."`. Safe-`next` redirect, same as `ModuleReleaseQuarantineView`.
  - API: `@action(detail=True, methods=["post"], url_path="retry-update", verb="manage")` → `{"retried_targets": [...], "module": ModuleSerializer}`. Schema via `inline_serializer("ModuleUpdateRetry", …)`.

- [ ] **Step 1: Write failing tests**

```python
def test_retry_failed_resets_and_audits(fm, station, staff_user)  # → UPDATING, tf=0, phase "", 1 RETRY with user
def test_retry_then_status_is_pending(...)  # module_update_status(...).reason_text == "Update ausstehend"
def test_retry_noop_for_not_updatable_and_quarantined(...)  # parametrize → [] and no audit
def test_retry_restarts_drain(...)  # station dfu-capable+online → drain.pending_updates(station) == [cs]
def test_retry_view_member_forbidden(client, member_user, ...)  # 403, mirrors test_release_quarantine_member_forbidden
def test_retry_view_get_not_allowed(...)  # 405
def test_retry_view_staff_redirects_to_detail(...)
# tests/test_api_module_firmware_modules.py
def test_api_retry_update_staff(api_topology, bearer, ...)  # 200, retried_targets == ["26.09.15-01"], module.update_status.state == "pending"
def test_api_retry_update_member_forbidden(...)  # 403
```

- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(module-fw): retry action for failed module updates (GUI + API)`

---

### Task 8: API serializers + OpenAPI

**Files:**
- Modify: `apps/api/module_firmware_serializers.py` (`ModuleSerializer`, `ModuleListSerializer`, `ModuleFirmwareReleaseSerializer`)
- Modify: `apps/api/module_firmware_views.py` (`ModuleViewSet.get_queryset` prefetch if needed)
- Modify: `config/settings/base.py` `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` only if the collision test demands it
- Test: extend `tests/test_api_module_firmware_read.py`, `tests/test_api_module_firmware_schema.py`, `tests/test_api_module_firmware_releases.py`

**Interfaces:**
- Consumes: `bulk_update_status`, `UpdateState`, `REASON_CODES`, `Phase`
- Produces: `ModuleSerializer` fields `+ "update_status", "reported_boot_key"`. `update_status` = `_UpdateStatusSerializer` with `state` (ChoiceField UpdateState), `phase` (ChoiceField Phase + "", allow_blank), `reason_code` (ChoiceField REASON_CODES + "", allow_blank), `reason_text` (CharField), `since` (DateTimeField, allow_null). `ModuleListSerializer.to_representation` computes `bulk_update_status(items)` once and passes it via `to_representation(m, release_keys=…, statuses=…)`. Retrieve falls back to `module_update_status`. `ModuleFirmwareReleaseSerializer.fields += ["boot_key_hash"]`. The `firmware_convergence` filter accepts the new values automatically (model choices; pin with a test).

- [ ] **Step 1: Write failing tests**

```python
def test_module_read_has_update_status_and_boot_key(...)  # keys == {"state","phase","reason_code","reason_text","since"}
def test_module_list_update_status_no_n_plus_one(django_assert_max_num_queries, ...)  # page with 10 modules ≤ same bound as with 1
def test_filter_firmware_convergence_failed_and_not_updatable(...)  # ?firmware_convergence=failed → only that module; not_updatable too
def test_release_read_has_boot_key_hash(...)
def test_schema_documents_update_status_enums():  # components contain enum with "not_updatable","running","waiting"; reason enum has "boot_key_mismatch","agent_no_module_ota"; phase enum has "verifying"
def test_schema_has_retry_update_path():  # "/api/v1/…/modules/{id}/retry-update/" POST (match existing path-prefix used in test_schema_includes_module_firmware_operations)
```

Existing `test_schema_generation_emits_no_enum_collision_warning` / `..._warning_count_does_not_grow` must stay green. Name the enums explicitly (`ModuleUpdateStateEnum`, `ModuleUpdatePhaseEnum`, `ModuleUpdateReasonEnum`) via `ENUM_NAME_OVERRIDES` if `state`/`phase` collide with other `state` enums.

- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run** `python -m pytest -q tests/test_api_module_firmware_*.py` → PASS.
- [ ] **Step 5: Commit** — `feat(api): module update_status, reported_boot_key, release boot_key_hash`

---

### Task 9: UI — pill, list column, detail, station card, live polling (pixel)

**REQUIRED:** the implementer (pixel agent) **must invoke `Skill("frontend-design")`** before touching templates/CSS.

**Files:**
- Create: `apps/module_firmware/templates/module_firmware/_update_status_pill.html`
- Create: `apps/module_firmware/views.py::ModuleUpdateStatusPartialView` (GET), URL `module_firmware:module_update_status` → `<str:uid>/update-status/` (next to the other `<str:uid>/…` sub-paths)
- Modify: `module_list.html` (new column "Firmware"), `ModuleListView.get_context_data` (`statuses = bulk_update_status(page modules)`, attach as `module.update_status` per row)
- Modify: `module_detail.html` + `ModuleDetailView` (`ctx["update_status"]`, `ctx["boot_key_is_dev"]`)
- Modify: `_convergence_pill.html` (styles for `failed` → `pill-error`, `not_updatable` → `pill-warn`)
- Modify: `apps/control/templates/control/_module_card.html` + `apps/control/views.py::StationControlView` (attach `m.update_status` via `bulk_update_status` over tracked modules, with `station=station`)
- Modify: CSS file holding `.fw-*` / `.cp-fw-updating` styles (find with `grep -rn "cp-fw-updating" static/ apps/*/static/`)
- Test: `tests/module_firmware/test_views_update_status.py`, extend `tests/module_firmware/test_views_list.py`, `test_views_detail.py`, `tests/test_control_firmware_updating.py`

**Interfaces:**
- Consumes: `UpdateStatus` (`state`, `short_label`, `reason_text`, `is_live`, `phase`, `since`), `services.retry_failed` URL, `mcuboot.is_dev_key`
- Produces: `_update_status_pill.html` expects `module` and `status`, plus an optional `compact` flag (list/card: short label + `title="{{ status.reason_text }}"`; detail: pill + full reason line). The root element carries `hx-get="{% url 'module_firmware:module_update_status' module.uid %}" hx-trigger="every 5s" hx-swap="outerHTML"` **only when `status.is_live`**, so polling stops by itself. The partial view renders the same template (`?compact=1` passes through), uses area `modules`/view, and returns 404 for an unknown uid.

UI rules:
- List: column header `Firmware`, compact pill, reason as tooltip. No polling (a reload is enough).
- Detail (Firmware panel): the pill + full `reason_text` replace the bare `_convergence_pill` in the panel head. If `status.state == "failed"` and `can.modules.manage`: a POST form to `module_retry_update` with button **`Erneut versuchen`** and `data-confirm` text `Update für {{ uid }} erneut versuchen?`. The quarantine note/button stays as is. New grid cell `Bootloader-Schlüssel`: `{{ module.reported_boot_key|default:"—" }}` (mono), plus a `Dev-Key` pill when `boot_key_is_dev`.
- Audit trail: new event types render via `get_event_type_display`. No template change is needed beyond checking readability (messages are German, Task 4/5).
- Station card: compact pill next to `_pills.html` in the header when `m.tracked_module`, polling while live. The existing Alpine `cp-fw-updating` banner **stays** as the operator-lock notice (WS-live). To avoid contradictions, the pill is hidden (`x-show="!isUpdating(...)"`) while the banner is shown, so during a live flash only the banner speaks. After the flash, the next poll shows the server state.
- All new texts German; `{% comment %}` only.

- [ ] **Step 1: Write failing tests**

```python
def test_list_shows_firmware_column_with_reason_tooltip(client, applicant_user, ...)  # "Wartet" + title contains "Image zu alt"
def test_list_update_status_constant_queries(django_assert_max_num_queries, ...)
def test_detail_shows_full_reason_and_retry_button_for_staff(client, staff_user, ...)  # failed row → "Fehler: …" + "Erneut versuchen"
def test_detail_no_retry_button_for_member(...)
def test_detail_no_retry_button_for_not_updatable(...)
def test_detail_shows_boot_key_and_dev_badge(...)  # "e30466f6b8470c1f" + "Dev-Key"
def test_partial_polls_only_while_live(client, applicant_user, ...)  # pending → 'hx-trigger="every 5s"' present; current → absent
def test_partial_unknown_uid_404(...)
def test_partial_anonymous_redirects(...)
def test_control_card_shows_update_pill(client, applicant_user, ...)  # tracked module waiting → "Wartet" in panel HTML
def test_templates_have_no_multiline_hash_comments():  # run the existing template guard (find its test/command and include it)
```

- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement** (frontend-design skill first). **Step 4: Run** `python -m pytest -q tests/module_firmware tests/test_control_*.py` → PASS.
- [ ] **Step 5: Manual check:** `python manage.py runserver` with seeded data (one module per state). Look at list, detail and control panel in light/dark. Screenshot for the PR.
- [ ] **Step 6: Commit** — `feat(module-fw): update state pill in module list, detail and station card`

---

### Task 10: E2E acceptance (probe) — fake agent through the real endpoints

**Files:**
- Create: `tests/module_firmware/test_update_state_e2e.py` (pattern: `tests/module_firmware/test_reconcile_e2e.py`: `apply_inventory` + signed device requests)

**Interfaces:**
- Consumes: everything above. No production code, except fixes for bugs found.

- [ ] **Step 1: Write the acceptance tests (spec §11)**

```python
def test_ac1_old_agent_shows_waiting_not_updating(...)
    # agent_capabilities={} + target + release → API update_status.reason_text ==
    # "Wartet: Station-Agent kann keine Module flashen (Image zu alt)"; detail HTML contains it
def test_ac2_phases_visible_and_audited_once(...)
    # check×3, downloading×2, flashing, verifying×2, heartbeat w/ target version, commit
    # → after each phase API state=="running" with matching phase; audit sequence ==
    # [INSTRUCTED, STARTED, PHASE, PHASE, SUCCESS] (no duplicates)
def test_ac3_five_transients_failed_then_retry(...)
    # 5× (check, status failed "dfu timeout") → reason_text == "Fehler: dfu timeout", drain.pending_updates == [],
    # exactly 1 GAVE_UP; check → 204; staff POST retry → "Update ausstehend", 1 RETRY; check → 200
def test_ac4_dev_key_vs_prod_release(...)
    # release boot_key_hash = other-signed fixture prefix; inventory boot_key = DEV prefix
    # 20× (heartbeat inventory + check) → every check 204, pending_updates == [], state not_updatable,
    # reason_text == Dev text, exactly 1 MODULE_NOT_UPDATABLE
def test_ac4b_bench_reflash_makes_updatable_again(...)  # inventory now reports release prefix → pending, 1 UPDATABLE_AGAIN, check 200
def test_ac5_no_boot_key_legacy_path_with_hint(...)
    # no boot_key in identity; 3× (check, downloading, rolled_back) → quarantined; MODULE_QUARANTINED message and
    # API reason_text contain KEY_UNKNOWN_HINT
def test_ac7_visibility_unchanged(...)  # anonymous → redirect on list/detail/partial; applicant sees state, no retry button
```

- [ ] **Step 2: Run** `python -m pytest -q tests/module_firmware/test_update_state_e2e.py` → PASS (fix the owning task's code for any failure, with a regression test in that task's test file).
- [ ] **Step 3: Full suite + lint:** `python -m pytest -q` → all green; `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .` → clean; `python manage.py makemigrations --check --dry-run` → no changes.
- [ ] **Step 4: Commit** — `test(module-fw): E2E acceptance for update state + lifecycle audit`

---

## Delivery

One PR (spec §12 PR 1): spec + both plans + code on `feature/module-fw-update-state`. After merge, run `python manage.py backfill_release_boot_keys` once on prod (optional, cheap). User-facing closing note in the PR (spec §12): a module with a dev-key bootloader has two ways out: remove the target, or reflash the bootloader with the prod key via SWD at the bench. Squash-merge only after user approval.
