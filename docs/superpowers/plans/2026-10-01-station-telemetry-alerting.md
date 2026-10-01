# Station Telemetry + Alerting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect richer boot/power/storage/slot telemetry in `station_agent`, ship it in the heartbeat, store + display it server-side, and raise topology-routed alerts for unexpected reboots, power/undervoltage, and storage wear.

**Architecture:** The agent gains a `telemetry` block in the heartbeat payload, assembled by independent, feature-detecting collectors (boot-reason, power/throttle, storage-health) that gracefully degrade when an OS source is absent. The server accepts the optional `telemetry` block, persists it to a new `StationTelemetry` one-to-one model (extracted columns + raw blob), detects reboot transitions into the audit log, and the `monitoring` engine adds three new checks that reuse the existing topology-routed notification channels.

**Tech Stack:** Python 3.14, Django 6.0, DRF 3.17, pytest (`config.settings.test`). Agent is plain Python (stdlib + pyyaml). No new third-party deps expected.

**Spec:** `docs/superpowers/specs/2026-10-01-station-telemetry-alerting-design.md`

## Global Constraints

- Tests live top-level in `tests/test_*.py`; run with `python -m pytest -q` (settings `config.settings.test`).
- Django templates: multi-line `{# … #}` is **forbidden** — use `{% comment %} … {% endcomment %}` (CI guard rejects regressions).
- Agent collectors **must feature-detect and gracefully degrade** — a missing source (qemu / pre-merge / SD-not-eMMC / non-rpi) omits the field or returns `None`; it never raises out of a collector and never aborts the heartbeat.
- Heartbeat liveness must never depend on telemetry parsing: server-side ingest is wrapped so a malformed `telemetry` blob is logged and skipped, not fatal.
- `last_reboot_reason` enum is exactly: `clean | crash | watchdog | ota_rollback | undervoltage | unknown`.
- Newest stable dependency versions if any are added (check PyPI first) — none expected.
- One coherent PR for the whole phase; squash-merge.

## Review Focus

- **Malformed/partial telemetry blob** (e.g. `telemetry` present but `boot` missing, or a string where a dict is expected): server ingest must not 500 the heartbeat — covered in Task 10.
- **First-ever heartbeat (no stored boot_id):** must record boot_id without emitting a spurious REBOOT audit event / unexpected-reboot alert — covered in Task 10.
- **Second distinct unexpected reboot while the first alert is still unresolved:** must re-alert (per-reboot dedup, not per-station) — covered in Task 13.
- **SD card instead of eMMC:** `mmc extcsd` life/pre-eol unavailable → fields are `n/a`/null, dmesg I/O-error scan still runs, no crash — covered in Task 3.
- **`vcgencmd` / `mmc` / `state_dir` entirely absent (qemu/native-sim):** `collect_telemetry` returns a partial dict with no exception — covered in Task 6.

---

## Task 1: Agent config — add `state_dir`

**Files:**
- Modify: `station_agent/config.py`
- Test: `tests/test_agent_config_state_dir.py`

**Interfaces:**
- Produces: `AgentConfig.state_dir: str` (default `"/var/lib/station-agent"`), parsed from YAML key `state_dir`, not required by `validate()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_config_state_dir.py
import os
import textwrap

from station_agent.config import AgentConfig, load_config


def test_state_dir_defaults():
    cfg = AgentConfig()
    assert cfg.state_dir == "/var/lib/station-agent"


def test_state_dir_loaded_from_yaml(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(textwrap.dedent("""
        server_url: https://example.test
        station_id: 1
        ed25519_key_path: /tmp/key.pem
        state_dir: /data/agent-state
    """))
    monkeypatch.setenv("STATION_AGENT_CONFIG", str(cfg_file))
    cfg = load_config()
    assert cfg.state_dir == "/data/agent-state"


def test_state_dir_not_required_for_validate():
    cfg = AgentConfig(
        server_url="https://x", station_id=1, ed25519_key_path="/k.pem", state_dir=""
    )
    cfg.validate()  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_config_state_dir.py -q`
Expected: FAIL (`AgentConfig` has no attribute `state_dir`).

- [ ] **Step 3: Implement**

In `station_agent/config.py`, add to the `AgentConfig` dataclass (near `download_dir`):

```python
    state_dir: str = "/var/lib/station-agent"
```

In `load_config()`, where other fields are read from `data`, add:

```python
        state_dir=str(data.get("state_dir", "/var/lib/station-agent")),
```

Do **not** add it to `validate()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_config_state_dir.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/config.py tests/test_agent_config_state_dir.py
git commit -m "feat(agent): add state_dir config for persistent telemetry state"
```

---

## Task 2: Agent power/throttle collector

**Files:**
- Create: `station_agent/power.py`
- Test: `tests/test_agent_power.py`

**Interfaces:**
- Produces: `read_throttle() -> dict | None`. Returns `None` if `vcgencmd` is absent. On success returns `{"throttled_hex": str, "undervoltage_now": bool, "undervoltage_occurred": bool, "throttled_now": bool, "throttled_occurred": bool, "freq_capped_now": bool, "freq_capped_occurred": bool}`.
- Produces: `parse_throttled(value: int) -> dict` (pure, for tests).

Bit meanings (rpi `vcgencmd get_throttled`): bit0 under-voltage now, bit1 freq-capped now, bit2 throttled now, bit3 soft-temp-limit now; bit16 under-voltage occurred, bit17 freq-capped occurred, bit18 throttled occurred, bit19 soft-temp-limit occurred.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_power.py
from unittest import mock

from station_agent import power


def test_parse_throttled_undervoltage_now_and_occurred():
    # bit0 (now) + bit16 (occurred)
    result = power.parse_throttled(0x10001)
    assert result["undervoltage_now"] is True
    assert result["undervoltage_occurred"] is True
    assert result["throttled_now"] is False


def test_parse_throttled_throttled_occurred_only():
    result = power.parse_throttled(0x40000)  # bit18
    assert result["throttled_occurred"] is True
    assert result["throttled_now"] is False


def test_parse_throttled_clean():
    result = power.parse_throttled(0x0)
    assert not any(result.values())


def test_read_throttle_absent_binary_returns_none():
    with mock.patch("shutil.which", return_value=None):
        assert power.read_throttle() is None


def test_read_throttle_parses_vcgencmd_output():
    with mock.patch("shutil.which", return_value="/usr/bin/vcgencmd"), \
         mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="throttled=0x50005\n")
        result = power.read_throttle()
    assert result["throttled_hex"] == "0x50005"
    assert result["undervoltage_now"] is True       # 0x5...=bit0
    assert result["undervoltage_occurred"] is True  # 0x5....=bit16
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_power.py -q`
Expected: FAIL (`No module named station_agent.power`).

- [ ] **Step 3: Implement**

```python
# station_agent/power.py
"""Raspberry Pi power/throttle telemetry via vcgencmd (feature-detected)."""

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

_BITS = {
    "undervoltage_now": 0,
    "freq_capped_now": 1,
    "throttled_now": 2,
    "undervoltage_occurred": 16,
    "freq_capped_occurred": 17,
    "throttled_occurred": 18,
}


def parse_throttled(value: int) -> dict:
    """Decode the vcgencmd get_throttled bitmask into named booleans."""
    return {name: bool(value & (1 << bit)) for name, bit in _BITS.items()}


def read_throttle() -> dict | None:
    """Return throttle/undervoltage state, or None if vcgencmd is unavailable."""
    if shutil.which("vcgencmd") is None:
        return None
    try:
        proc = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return None
        raw = proc.stdout.strip()  # "throttled=0x50005"
        hex_str = raw.split("=", 1)[1].strip()
        value = int(hex_str, 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        logger.debug("vcgencmd get_throttled failed: %s", exc)
        return None
    result = {"throttled_hex": hex_str}
    result.update(parse_throttled(value))
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_power.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/power.py tests/test_agent_power.py
git commit -m "feat(agent): vcgencmd throttle/undervoltage collector with graceful degrade"
```

---

## Task 3: Agent storage-health collector

**Files:**
- Create: `station_agent/storage.py`
- Test: `tests/test_agent_storage.py`

**Interfaces:**
- Produces: `read_storage_health() -> dict | None` → `{"root_device": str|None, "devices": [ {"name": str, "kind": "emmc"|"sd"|"unknown", "life_time_a_pct": int|None, "life_time_b_pct": int|None, "pre_eol": "normal"|"warning"|"urgent"|"n/a", "io_error_count": int} ]}`. Returns `None` only if no mmc device can be found at all.
- Produces pure parsers: `parse_extcsd(text: str) -> dict` → `{"life_time_a_pct", "life_time_b_pct", "pre_eol"}`; `count_mmc_io_errors(dmesg_text: str) -> int`.
- Produces: `life_time_band_to_pct(code: int) -> int|None` (eMMC `DEVICE_LIFE_TIME_EST_TYP`: 0x01→10%, 0x02→20% … 0x0A→100%, 0x0B→">100% (EOL)"→100; 0x00→None/unknown).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_storage.py
from station_agent import storage

EXTCSD_SAMPLE = """
eMMC Life Time Estimation A [EXT_CSD_DEVICE_LIFE_TIME_EST_TYP_A]: 0x02
eMMC Life Time Estimation B [EXT_CSD_DEVICE_LIFE_TIME_EST_TYP_B]: 0x01
eMMC Pre EOL information [EXT_CSD_PRE_EOL_INFO]: 0x01
"""

DMESG_SAMPLE = """
[    1.23] mmc0: new HS400 MMC card
[ 9000.00] mmcblk0: error -110 transferring data
[ 9001.00] blk_update_request: I/O error, dev mmcblk0, sector 123
"""


def test_life_time_band_to_pct():
    assert storage.life_time_band_to_pct(0x01) == 10
    assert storage.life_time_band_to_pct(0x0A) == 100
    assert storage.life_time_band_to_pct(0x00) is None


def test_parse_extcsd():
    result = storage.parse_extcsd(EXTCSD_SAMPLE)
    assert result["life_time_a_pct"] == 20
    assert result["life_time_b_pct"] == 10
    assert result["pre_eol"] == "normal"


def test_parse_extcsd_pre_eol_urgent():
    result = storage.parse_extcsd(
        "eMMC Pre EOL information [EXT_CSD_PRE_EOL_INFO]: 0x03\n"
    )
    assert result["pre_eol"] == "urgent"


def test_count_mmc_io_errors():
    assert storage.count_mmc_io_errors(DMESG_SAMPLE) == 2


def test_count_mmc_io_errors_none():
    assert storage.count_mmc_io_errors("[0.0] clean boot\n") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_storage.py -q`
Expected: FAIL (`No module named station_agent.storage`).

- [ ] **Step 3: Implement**

```python
# station_agent/storage.py
"""SD/eMMC storage-health telemetry (feature-detected, graceful degrade)."""

import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

_PRE_EOL = {0x01: "normal", 0x02: "warning", 0x03: "urgent"}
_LIFE_RE = re.compile(r"LIFE_TIME_EST_TYP_([AB])\]?:\s*0x([0-9a-fA-F]+)")
_PRE_EOL_RE = re.compile(r"PRE_EOL_INFO\]?:\s*0x([0-9a-fA-F]+)")
_IO_ERR_RE = re.compile(r"(I/O error.*mmcblk|mmcblk\d+: error|mmc\d+:.*error)", re.I)


def life_time_band_to_pct(code: int) -> int | None:
    """eMMC life-time band (0x01..0x0B) → percent consumed; 0x00/unknown → None."""
    if code in (0x00, None):
        return None
    if code >= 0x0B:
        return 100
    return code * 10


def parse_extcsd(text: str) -> dict:
    """Parse `mmc extcsd read` output for life-time + PRE_EOL."""
    result = {"life_time_a_pct": None, "life_time_b_pct": None, "pre_eol": "n/a"}
    for which, hexval in _LIFE_RE.findall(text):
        pct = life_time_band_to_pct(int(hexval, 16))
        result["life_time_a_pct" if which == "A" else "life_time_b_pct"] = pct
    m = _PRE_EOL_RE.search(text)
    if m:
        result["pre_eol"] = _PRE_EOL.get(int(m.group(1), 16), "n/a")
    return result


def count_mmc_io_errors(dmesg_text: str) -> int:
    """Count mmc/I-O-error lines in dmesg output (SD + eMMC)."""
    return sum(1 for line in dmesg_text.splitlines() if _IO_ERR_RE.search(line))


def _root_block_device() -> str | None:
    """Resolve the mmc block device backing '/', e.g. 'mmcblk0'. None if not mmc."""
    try:
        st = os.stat("/")
        major, minor = os.major(st.st_dev), os.minor(st.st_dev)
        # Walk /sys/class/block to find the parent disk of the root partition.
        for name in os.listdir("/sys/class/block"):
            if not name.startswith("mmcblk"):
                continue
            dev_path = f"/sys/class/block/{name}/dev"
            if not os.path.exists(dev_path):
                continue
            # parent disk (strip pN partition suffix)
            disk = re.sub(r"p\d+$", "", name)
            # match either the partition or its disk device number
            with open(dev_path) as f:
                blk_major, blk_minor = (int(x) for x in f.read().strip().split(":"))
            if blk_major == major:
                return disk
    except OSError as exc:
        logger.debug("root block device resolution failed: %s", exc)
    return None


def _device_kind(disk: str) -> str:
    """Classify an mmc disk as emmc/sd/unknown via /sys type."""
    try:
        with open(f"/sys/class/block/{disk}/device/type") as f:
            t = f.read().strip().upper()
        if t == "MMC":
            return "emmc"
        if t == "SD":
            return "sd"
    except OSError:
        pass
    return "unknown"


def _read_extcsd(disk: str) -> dict:
    if shutil.which("mmc") is None:
        return {"life_time_a_pct": None, "life_time_b_pct": None, "pre_eol": "n/a"}
    try:
        proc = subprocess.run(
            ["mmc", "extcsd", "read", f"/dev/{disk}"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return {"life_time_a_pct": None, "life_time_b_pct": None, "pre_eol": "n/a"}
        return parse_extcsd(proc.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("mmc extcsd read failed for %s: %s", disk, exc)
        return {"life_time_a_pct": None, "life_time_b_pct": None, "pre_eol": "n/a"}


def _read_dmesg() -> str:
    try:
        proc = subprocess.run(
            ["dmesg"], capture_output=True, text=True, timeout=10
        )
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def read_storage_health() -> dict | None:
    """Collect storage health for the root mmc device. None if no mmc device."""
    disk = _root_block_device()
    if disk is None:
        return None
    kind = _device_kind(disk)
    io_errors = count_mmc_io_errors(_read_dmesg())
    device = {"name": disk, "kind": kind, "io_error_count": io_errors}
    if kind == "emmc":
        device.update(_read_extcsd(disk))
    else:  # sd / unknown: no reliable extcsd life data
        device.update({"life_time_a_pct": None, "life_time_b_pct": None, "pre_eol": "n/a"})
    return {"root_device": disk, "devices": [device]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_storage.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/storage.py tests/test_agent_storage.py
git commit -m "feat(agent): SD/eMMC storage-health collector (extcsd + dmesg scan)"
```

---

## Task 4: Agent boot-reason logic (pure `compute_reboot_reason`)

**Files:**
- Create: `station_agent/bootinfo.py`
- Test: `tests/test_agent_bootinfo_reason.py`

**Interfaces:**
- Produces: `compute_reboot_reason(evidence: dict) -> str` — pure function. `evidence` keys: `pstore_crash: bool`, `ota_rollback: bool`, `watchdog: bool`, `undervoltage_occurred: bool`, `clean_marker: bool`. Returns one of `crash|ota_rollback|watchdog|undervoltage|clean|unknown`.

Priority: crash > ota_rollback > watchdog > undervoltage (only if not clean) > clean > unknown.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_bootinfo_reason.py
import pytest

from station_agent.bootinfo import compute_reboot_reason

BASE = dict(pstore_crash=False, ota_rollback=False, watchdog=False,
            undervoltage_occurred=False, clean_marker=False)


def ev(**kw):
    return {**BASE, **kw}


@pytest.mark.parametrize("evidence,expected", [
    (ev(pstore_crash=True), "crash"),
    (ev(pstore_crash=True, clean_marker=True), "crash"),       # crash wins
    (ev(ota_rollback=True), "ota_rollback"),
    (ev(watchdog=True), "watchdog"),
    (ev(undervoltage_occurred=True), "undervoltage"),
    (ev(undervoltage_occurred=True, clean_marker=True), "clean"),  # clean shutdown wins over UV-occurred
    (ev(clean_marker=True), "clean"),
    (ev(), "unknown"),
])
def test_compute_reboot_reason(evidence, expected):
    assert compute_reboot_reason(evidence) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_bootinfo_reason.py -q`
Expected: FAIL (`No module named station_agent.bootinfo`).

- [ ] **Step 3: Implement**

Create `station_agent/bootinfo.py` with (the rest of the module is added in Task 5):

```python
# station_agent/bootinfo.py
"""Boot / reboot-reason detection — agent-owned per telemetry contract."""

import logging

logger = logging.getLogger(__name__)


def compute_reboot_reason(evidence: dict) -> str:
    """Classify why the previous boot ended, from collected evidence.

    Priority: crash > ota_rollback > watchdog > undervoltage > clean > unknown.
    A clean systemd shutdown overrides a lingering 'undervoltage occurred' bit.
    """
    if evidence.get("pstore_crash"):
        return "crash"
    if evidence.get("ota_rollback"):
        return "ota_rollback"
    if evidence.get("watchdog"):
        return "watchdog"
    if evidence.get("clean_marker"):
        return "clean"
    if evidence.get("undervoltage_occurred"):
        return "undervoltage"
    return "unknown"
```

Note: `clean_marker` is checked before `undervoltage_occurred` so a clean shutdown wins (matches the test expectation), while crash/watchdog/ota still override a clean marker.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_bootinfo_reason.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/bootinfo.py tests/test_agent_bootinfo_reason.py
git commit -m "feat(agent): pure reboot-reason classifier (contract enum)"
```

---

## Task 5: Agent boot detection + clean-shutdown marker + evidence gathering

**Files:**
- Modify: `station_agent/bootinfo.py`
- Modify: `station_agent/agent.py` (write clean marker on graceful shutdown)
- Test: `tests/test_agent_bootinfo_detect.py`

**Interfaces:**
- Consumes: `compute_reboot_reason` (Task 4), `power.read_throttle` (Task 2), `bootloader.get_env`/`get_bootloader` (existing).
- Produces:
  - `read_boot_id() -> str | None` (reads `/proc/sys/kernel/random/boot_id`).
  - `mark_clean_shutdown(state_dir: str) -> None` / `clean_marker_present(state_dir: str) -> bool` / `_clear_clean_marker(state_dir)`.
  - `detect_boot(state_dir, bootloader=None) -> dict` → `{"boot_id": str|None, "boot_count": int, "reboot_reason": str}`. On a new boot_id (or no prior state): compute reason, increment count, persist `<state_dir>/boot_state.json`, consume one-shot evidence (delete pstore dmesg records, clear clean marker). On unchanged boot_id: return persisted values without recompute. Degrades to `{boot_count:0, reboot_reason:"unknown"}` if state_dir unusable.
  - `_gather_evidence(bootloader) -> dict` for `compute_reboot_reason` (pstore scan, bootloader-env rollback tuple, dmesg watchdog, throttle undervoltage-occurred, clean marker).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_bootinfo_detect.py
import json
from unittest import mock

from station_agent import bootinfo


def test_detect_boot_first_time_records_without_reboot_event(tmp_path):
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"), \
         mock.patch.object(bootinfo, "_gather_evidence", return_value={}):
        result = bootinfo.detect_boot(str(tmp_path))
    assert result["boot_id"] == "boot-A"
    assert result["boot_count"] == 1
    state = json.loads((tmp_path / "boot_state.json").read_text())
    assert state["boot_id"] == "boot-A"
    assert state["boot_count"] == 1


def test_detect_boot_same_boot_id_no_increment(tmp_path):
    (tmp_path / "boot_state.json").write_text(
        json.dumps({"boot_id": "boot-A", "boot_count": 3, "reboot_reason": "clean"})
    )
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"):
        result = bootinfo.detect_boot(str(tmp_path))
    assert result["boot_count"] == 3
    assert result["reboot_reason"] == "clean"


def test_detect_boot_new_boot_id_increments_and_classifies(tmp_path):
    (tmp_path / "boot_state.json").write_text(
        json.dumps({"boot_id": "boot-A", "boot_count": 3, "reboot_reason": "clean"})
    )
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-B"), \
         mock.patch.object(bootinfo, "_gather_evidence",
                           return_value={"pstore_crash": True}):
        result = bootinfo.detect_boot(str(tmp_path))
    assert result["boot_id"] == "boot-B"
    assert result["boot_count"] == 4
    assert result["reboot_reason"] == "crash"


def test_detect_boot_unwritable_state_dir_degrades():
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"), \
         mock.patch.object(bootinfo, "_gather_evidence", return_value={}), \
         mock.patch("station_agent.bootinfo._load_state", side_effect=OSError), \
         mock.patch("station_agent.bootinfo._save_state", side_effect=OSError):
        result = bootinfo.detect_boot("/nonexistent/path")
    assert result["boot_count"] == 0
    assert result["reboot_reason"] == "unknown"


def test_clean_marker_roundtrip(tmp_path):
    assert bootinfo.clean_marker_present(str(tmp_path)) is False
    bootinfo.mark_clean_shutdown(str(tmp_path))
    assert bootinfo.clean_marker_present(str(tmp_path)) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_bootinfo_detect.py -q`
Expected: FAIL (functions not defined).

- [ ] **Step 3: Implement**

Append to `station_agent/bootinfo.py`:

```python
import json
import os
import subprocess

from station_agent import power

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_STATE_FILE = "boot_state.json"
_CLEAN_MARKER = "clean_shutdown.marker"
_PSTORE_DIR = "/sys/fs/pstore"


def read_boot_id() -> str | None:
    try:
        with open(BOOT_ID_PATH) as f:
            return f.read().strip()
    except OSError:
        return None


def _state_path(state_dir: str) -> str:
    return os.path.join(state_dir, _STATE_FILE)


def _load_state(state_dir: str) -> dict:
    with open(_state_path(state_dir)) as f:
        return json.load(f)


def _save_state(state_dir: str, state: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    tmp = _state_path(state_dir) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _state_path(state_dir))


def mark_clean_shutdown(state_dir: str) -> None:
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, _CLEAN_MARKER), "w") as f:
            f.write("1")
    except OSError as exc:
        logger.debug("could not write clean-shutdown marker: %s", exc)


def clean_marker_present(state_dir: str) -> bool:
    return os.path.exists(os.path.join(state_dir, _CLEAN_MARKER))


def _clear_clean_marker(state_dir: str) -> None:
    try:
        os.remove(os.path.join(state_dir, _CLEAN_MARKER))
    except OSError:
        pass


def _pstore_has_crash() -> bool:
    try:
        return any(name.startswith("dmesg-") for name in os.listdir(_PSTORE_DIR))
    except OSError:
        return False


def _consume_pstore() -> None:
    try:
        for name in os.listdir(_PSTORE_DIR):
            if name.startswith("dmesg-"):
                try:
                    os.remove(os.path.join(_PSTORE_DIR, name))
                except OSError:
                    pass
    except OSError:
        pass


def _bootloader_rollback(bootloader) -> bool:
    """Mirror the agent commit-protocol tuple: upgrade_available=0 & bootcount!=0."""
    try:
        from station_agent import bootloader as bl_mod
        ua = bl_mod.get_env(bootloader, "upgrade_available")
        bc = bl_mod.get_env(bootloader, "bootcount")
        return ua == "0" and bc not in (None, "0")
    except Exception as exc:  # noqa: BLE001 - never fail boot detection
        logger.debug("bootloader rollback probe failed: %s", exc)
        return False


def _dmesg_watchdog() -> bool:
    try:
        proc = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return False
        text = proc.stdout.lower()
        return "bcm2835-wdt" in text or "watchdog" in text and "reset" in text
    except (OSError, subprocess.SubprocessError):
        return False


def _gather_evidence(bootloader, state_dir: str) -> dict:
    throttle = power.read_throttle() or {}
    return {
        "pstore_crash": _pstore_has_crash(),
        "ota_rollback": _bootloader_rollback(bootloader),
        "watchdog": _dmesg_watchdog(),
        "undervoltage_occurred": bool(throttle.get("undervoltage_occurred")),
        "clean_marker": clean_marker_present(state_dir),
    }


def detect_boot(state_dir: str, bootloader=None) -> dict:
    """Detect current boot, classify a reboot once per new boot_id."""
    boot_id = read_boot_id()
    try:
        state = _load_state(state_dir)
    except (OSError, ValueError):
        state = {}

    if state.get("boot_id") == boot_id and boot_id is not None:
        return {
            "boot_id": boot_id,
            "boot_count": state.get("boot_count", 0),
            "reboot_reason": state.get("reboot_reason", "unknown"),
        }

    # New boot (or first run / unreadable state).
    evidence = _gather_evidence(bootloader, state_dir)
    reason = compute_reboot_reason(evidence)
    count = state.get("boot_count", 0) + 1
    new_state = {"boot_id": boot_id, "boot_count": count, "reboot_reason": reason}
    try:
        _save_state(state_dir, new_state)
        _consume_pstore()
        _clear_clean_marker(state_dir)
    except OSError as exc:
        logger.debug("boot state persistence failed, degrading: %s", exc)
        return {"boot_id": boot_id, "boot_count": 0, "reboot_reason": "unknown"}
    return new_state
```

In `station_agent/agent.py`, in the graceful shutdown path (where `self._shutdown` is set / after the main loop exits on SIGTERM), call:

```python
from station_agent import bootinfo
bootinfo.mark_clean_shutdown(config.state_dir)
```

Place it so it runs on normal termination (SIGTERM handler or the `finally` after the heartbeat loop), guarded by the config being available.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_bootinfo_detect.py -q`
Expected: PASS.

- [ ] **Step 5: Run full agent test slice**

Run: `python -m pytest tests/test_agent_bootinfo_reason.py tests/test_agent_bootinfo_detect.py tests/test_agent_power.py tests/test_agent_storage.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add station_agent/bootinfo.py station_agent/agent.py tests/test_agent_bootinfo_detect.py
git commit -m "feat(agent): boot detection, clean-shutdown marker, reboot-reason evidence"
```

---

## Task 6: Agent telemetry orchestrator + heartbeat wiring

**Files:**
- Create: `station_agent/telemetry.py`
- Modify: `station_agent/heartbeat.py` (add `telemetry` to `collect_system_info`)
- Test: `tests/test_agent_telemetry.py`

**Interfaces:**
- Consumes: `bootinfo.detect_boot`, `power.read_throttle`, `storage.read_storage_health`, `bootloader.get_active_slot`/`get_bootloader`, `inventory.get_current_version`.
- Produces: `collect_telemetry(config) -> dict` with optional keys `boot`, `power`, `slot`, `storage` — each omitted if its collector returns falsy/raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_telemetry.py
from unittest import mock

from station_agent import telemetry
from station_agent.config import AgentConfig


def _cfg(tmp_path):
    return AgentConfig(server_url="https://x", station_id=1,
                       ed25519_key_path="/k.pem", state_dir=str(tmp_path))


def test_collect_telemetry_all_sources_present(tmp_path):
    with mock.patch("station_agent.telemetry.bootinfo.detect_boot",
                    return_value={"boot_id": "b", "boot_count": 2, "reboot_reason": "clean"}), \
         mock.patch("station_agent.telemetry.power.read_throttle",
                    return_value={"throttled_hex": "0x0", "undervoltage_now": False}), \
         mock.patch("station_agent.telemetry.storage.read_storage_health",
                    return_value={"root_device": "mmcblk0", "devices": []}), \
         mock.patch("station_agent.telemetry._collect_slot",
                    return_value={"active_slot": "a", "image_version": "v1"}):
        result = telemetry.collect_telemetry(_cfg(tmp_path))
    assert result["boot"]["reboot_reason"] == "clean"
    assert result["power"]["throttled_hex"] == "0x0"
    assert result["storage"]["root_device"] == "mmcblk0"
    assert result["slot"]["active_slot"] == "a"


def test_collect_telemetry_graceful_when_sources_missing(tmp_path):
    with mock.patch("station_agent.telemetry.bootinfo.detect_boot",
                    side_effect=RuntimeError("boom")), \
         mock.patch("station_agent.telemetry.power.read_throttle", return_value=None), \
         mock.patch("station_agent.telemetry.storage.read_storage_health", return_value=None), \
         mock.patch("station_agent.telemetry._collect_slot", return_value={}):
        result = telemetry.collect_telemetry(_cfg(tmp_path))
    # No exception; absent sources omitted.
    assert "power" not in result
    assert "storage" not in result
    assert "boot" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_telemetry.py -q`
Expected: FAIL (`No module named station_agent.telemetry`).

- [ ] **Step 3: Implement**

```python
# station_agent/telemetry.py
"""Assemble the heartbeat telemetry block from feature-detected collectors."""

import logging

from station_agent import bootinfo, inventory, power, storage

logger = logging.getLogger(__name__)


def _safe(label, fn):
    """Run a collector; never raise. Returns its result or None on error."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - telemetry must never break heartbeat
        logger.debug("telemetry collector %s failed: %s", label, exc)
        return None


def _collect_slot(config) -> dict:
    from station_agent import bootloader
    out = {}
    try:
        bl = bootloader.get_bootloader(config)
        out["active_slot"] = bootloader.get_active_slot(bl)
    except Exception as exc:  # noqa: BLE001
        logger.debug("active slot probe failed: %s", exc)
        out["active_slot"] = None
    try:
        out["image_version"] = inventory.get_current_version()
    except Exception as exc:  # noqa: BLE001
        logger.debug("image version probe failed: %s", exc)
        out["image_version"] = ""
    return out


def collect_telemetry(config) -> dict:
    """Build the telemetry dict; each block is independently optional."""
    result = {}
    boot = _safe("boot", lambda: bootinfo.detect_boot(
        config.state_dir, _bootloader_or_none(config)))
    if boot:
        result["boot"] = boot
    pwr = _safe("power", power.read_throttle)
    if pwr:
        result["power"] = pwr
    stor = _safe("storage", storage.read_storage_health)
    if stor:
        result["storage"] = stor
    slot = _safe("slot", lambda: _collect_slot(config))
    if slot:
        result["slot"] = slot
    return result


def _bootloader_or_none(config):
    try:
        from station_agent import bootloader
        return bootloader.get_bootloader(config)
    except Exception:  # noqa: BLE001
        return None
```

In `station_agent/heartbeat.py`, add to `collect_system_info()`'s returned dict:

```python
        "telemetry": _collect_telemetry_safe(config),
```

and near the top of `heartbeat.py`:

```python
def _collect_telemetry_safe(config):
    from station_agent import telemetry
    try:
        return telemetry.collect_telemetry(config)
    except Exception:  # noqa: BLE001
        logger.debug("telemetry collection failed; sending empty telemetry")
        return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_telemetry.py -q`
Expected: PASS.

- [ ] **Step 5: Run the heartbeat/inventory tests to confirm no regression**

Run: `python -m pytest tests/test_heartbeat_inventory.py tests/test_heartbeat_variant.py -q`
Expected: PASS (new `telemetry` key is additive).

- [ ] **Step 6: Commit**

```bash
git add station_agent/telemetry.py station_agent/heartbeat.py tests/test_agent_telemetry.py
git commit -m "feat(agent): assemble telemetry block and ship it in the heartbeat"
```

---

## Task 7: Server — accept `telemetry` in the heartbeat serializer

**Files:**
- Modify: `apps/api/serializers.py` (`HeartbeatSerializer`)
- Test: `tests/test_heartbeat_telemetry_serializer.py`

**Interfaces:**
- Produces: `HeartbeatSerializer` accepts optional `telemetry` (dict). Absent → validates fine; present → available in `validated_data["telemetry"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_heartbeat_telemetry_serializer.py
from apps.api.serializers import HeartbeatSerializer

BASE = dict(hostname="h", os_version="o", uptime=1.0,
            module_versions={}, ip_address="10.0.0.1")


def test_serializer_accepts_telemetry():
    s = HeartbeatSerializer(data={**BASE, "telemetry": {"boot": {"boot_count": 2}}})
    assert s.is_valid(), s.errors
    assert s.validated_data["telemetry"]["boot"]["boot_count"] == 2


def test_serializer_telemetry_optional():
    s = HeartbeatSerializer(data=BASE)
    assert s.is_valid(), s.errors
    assert "telemetry" not in s.validated_data or s.validated_data.get("telemetry") in ({}, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_heartbeat_telemetry_serializer.py -q`
Expected: FAIL (`telemetry` dropped from validated_data).

- [ ] **Step 3: Implement**

In `apps/api/serializers.py`, add to `HeartbeatSerializer` (next to `inventory`):

```python
    telemetry = serializers.DictField(required=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_heartbeat_telemetry_serializer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/serializers.py tests/test_heartbeat_telemetry_serializer.py
git commit -m "feat(api): accept optional telemetry block in heartbeat serializer"
```

---

## Task 8: Server — `StationTelemetry` model + migration + REBOOT audit event type

**Files:**
- Modify: `apps/stations/models.py` (add `StationTelemetry`; add `REBOOT` to `StationAuditLog.EventType`)
- Create: `apps/stations/migrations/0022_stationtelemetry.py` (via `makemigrations`)
- Test: `tests/test_station_telemetry_model.py`

**Interfaces:**
- Produces: `StationTelemetry` model (fields per spec), `station.telemetry` reverse accessor; `StationAuditLog.EventType.REBOOT`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_station_telemetry_model.py
import pytest

from apps.stations.models import Station, StationTelemetry, StationAuditLog


@pytest.mark.django_db
def test_station_telemetry_defaults():
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(station=station)
    assert tel.boot_count == 0
    assert tel.io_error_count == 0
    assert station.telemetry == tel


@pytest.mark.django_db
def test_reboot_audit_event_type_exists():
    assert hasattr(StationAuditLog.EventType, "REBOOT")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_station_telemetry_model.py -q`
Expected: FAIL (`cannot import name StationTelemetry`).

- [ ] **Step 3: Implement**

In `apps/stations/models.py`, add `REBOOT = "reboot", _("Reboot")` to the `StationAuditLog.EventType` choices, and add the model (near `StationInventory`):

```python
class StationTelemetry(models.Model):
    """Latest telemetry snapshot reported by the agent (one per station)."""

    station = models.OneToOneField(
        Station, on_delete=models.CASCADE, related_name="telemetry"
    )
    data = models.JSONField(default=dict, blank=True)
    # boot
    boot_id = models.CharField(max_length=64, blank=True)
    boot_count = models.PositiveIntegerField(default=0)
    last_reboot_reason = models.CharField(max_length=16, blank=True)
    last_reboot_at = models.DateTimeField(null=True, blank=True)
    uptime_seconds = models.FloatField(null=True, blank=True)
    # power
    undervoltage_now = models.BooleanField(null=True)
    undervoltage_occurred = models.BooleanField(null=True)
    throttled_now = models.BooleanField(null=True)
    throttled_occurred = models.BooleanField(null=True)
    # slot / version / ota
    active_slot = models.CharField(max_length=1, blank=True)
    image_version = models.CharField(max_length=100, blank=True)
    last_ota_result = models.CharField(max_length=16, blank=True)
    # storage (worst-case summary across devices)
    worst_life_time_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    worst_pre_eol = models.CharField(max_length=8, blank=True)
    io_error_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Telemetry for {self.station}"
```

- [ ] **Step 4: Generate migration**

Run: `python manage.py makemigrations stations`
Expected: creates `apps/stations/migrations/0022_stationtelemetry.py` (and the audit event-type alteration). Verify the file is created.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_station_telemetry_model.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/stations/models.py apps/stations/migrations/0022_stationtelemetry.py tests/test_station_telemetry_model.py
git commit -m "feat(stations): StationTelemetry model + REBOOT audit event type"
```

---

## Task 9: Server — `ingest_telemetry` (upsert + reboot transition)

**Files:**
- Create: `apps/stations/ingest.py`
- Test: `tests/test_telemetry_ingest.py`

**Interfaces:**
- Consumes: `StationTelemetry`, `StationAuditLog` (Task 8).
- Produces: `ingest_telemetry(station, telemetry: dict) -> StationTelemetry | None`. Upserts the row from the blob; on `boot_id` change (with a non-empty prior boot_id) sets `last_reboot_at=now()` and writes a `REBOOT` `StationAuditLog` with the reason; first-ever boot_id records without a REBOOT event. Tolerates missing sub-blocks. Returns `None` (logged) if `telemetry` is not a dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_ingest.py
import pytest

from apps.stations.ingest import ingest_telemetry
from apps.stations.models import Station, StationTelemetry, StationAuditLog


@pytest.mark.django_db
def test_ingest_first_boot_no_reboot_event():
    station = Station.objects.create(name="OE5A")
    tel = ingest_telemetry(station, {
        "boot": {"boot_id": "b1", "boot_count": 1, "reboot_reason": "clean",
                 "uptime_seconds": 10.0},
    })
    assert tel.boot_id == "b1"
    assert tel.last_reboot_at is None
    assert not StationAuditLog.objects.filter(
        station=station, event_type=StationAuditLog.EventType.REBOOT).exists()


@pytest.mark.django_db
def test_ingest_boot_id_change_logs_reboot():
    station = Station.objects.create(name="OE5A")
    ingest_telemetry(station, {"boot": {"boot_id": "b1", "boot_count": 1,
                                        "reboot_reason": "clean"}})
    tel = ingest_telemetry(station, {"boot": {"boot_id": "b2", "boot_count": 2,
                                              "reboot_reason": "crash"}})
    assert tel.boot_id == "b2"
    assert tel.last_reboot_at is not None
    assert tel.last_reboot_reason == "crash"
    assert StationAuditLog.objects.filter(
        station=station, event_type=StationAuditLog.EventType.REBOOT).count() == 1


@pytest.mark.django_db
def test_ingest_extracts_power_slot_storage():
    station = Station.objects.create(name="OE5A")
    tel = ingest_telemetry(station, {
        "power": {"undervoltage_now": False, "undervoltage_occurred": True,
                  "throttled_now": False, "throttled_occurred": False},
        "slot": {"active_slot": "b", "image_version": "v9", "last_ota_result": "success"},
        "storage": {"root_device": "mmcblk0", "devices": [
            {"name": "mmcblk0", "kind": "emmc", "life_time_a_pct": 30,
             "life_time_b_pct": 10, "pre_eol": "warning", "io_error_count": 2}]},
    })
    assert tel.undervoltage_occurred is True
    assert tel.active_slot == "b"
    assert tel.image_version == "v9"
    assert tel.worst_life_time_pct == 30
    assert tel.worst_pre_eol == "warning"
    assert tel.io_error_count == 2


@pytest.mark.django_db
def test_ingest_malformed_blob_returns_none():
    station = Station.objects.create(name="OE5A")
    assert ingest_telemetry(station, "not-a-dict") is None
    assert ingest_telemetry(station, {"boot": "bad"}) is not None  # partial tolerated
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_telemetry_ingest.py -q`
Expected: FAIL (`No module named apps.stations.ingest`).

- [ ] **Step 3: Implement**

```python
# apps/stations/ingest.py
"""Persist agent-reported telemetry into StationTelemetry + audit reboots."""

import logging

from django.utils import timezone

from apps.stations.models import StationAuditLog, StationTelemetry

logger = logging.getLogger(__name__)

_PRE_EOL_RANK = {"normal": 0, "n/a": 0, "": 0, "warning": 1, "urgent": 2}


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _worst_storage(storage: dict):
    """Return (worst_life_time_pct, worst_pre_eol, io_error_count)."""
    devices = storage.get("devices") if isinstance(storage, dict) else None
    if not isinstance(devices, list):
        return None, "", 0
    worst_life = None
    worst_eol = ""
    io_errors = 0
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        for key in ("life_time_a_pct", "life_time_b_pct"):
            val = dev.get(key)
            if isinstance(val, int):
                worst_life = val if worst_life is None else max(worst_life, val)
        eol = dev.get("pre_eol") or ""
        if _PRE_EOL_RANK.get(eol, 0) > _PRE_EOL_RANK.get(worst_eol, 0):
            worst_eol = eol
        if isinstance(dev.get("io_error_count"), int):
            io_errors += dev["io_error_count"]
    return worst_life, worst_eol, io_errors


def ingest_telemetry(station, telemetry):
    """Upsert StationTelemetry; log a REBOOT audit event on boot_id change."""
    if not isinstance(telemetry, dict):
        logger.warning("Ignoring non-dict telemetry for station %s", station.pk)
        return None

    boot = _as_dict(telemetry.get("boot"))
    powerd = _as_dict(telemetry.get("power"))
    slot = _as_dict(telemetry.get("slot"))
    storaged = _as_dict(telemetry.get("storage"))

    tel, _created = StationTelemetry.objects.get_or_create(station=station)
    prev_boot_id = tel.boot_id
    new_boot_id = str(boot.get("boot_id") or "")

    reboot_detected = bool(prev_boot_id) and bool(new_boot_id) and new_boot_id != prev_boot_id

    tel.data = telemetry
    tel.boot_id = new_boot_id or prev_boot_id
    if isinstance(boot.get("boot_count"), int):
        tel.boot_count = boot["boot_count"]
    tel.last_reboot_reason = str(boot.get("reboot_reason") or tel.last_reboot_reason or "")
    if isinstance(boot.get("uptime_seconds"), (int, float)):
        tel.uptime_seconds = float(boot["uptime_seconds"])

    tel.undervoltage_now = powerd.get("undervoltage_now")
    tel.undervoltage_occurred = powerd.get("undervoltage_occurred")
    tel.throttled_now = powerd.get("throttled_now")
    tel.throttled_occurred = powerd.get("throttled_occurred")

    tel.active_slot = str(slot.get("active_slot") or "")[:1]
    tel.image_version = str(slot.get("image_version") or "")[:100]
    tel.last_ota_result = str(slot.get("last_ota_result") or "")[:16]

    worst_life, worst_eol, io_errors = _worst_storage(storaged)
    tel.worst_life_time_pct = worst_life
    tel.worst_pre_eol = worst_eol
    tel.io_error_count = io_errors

    if reboot_detected:
        tel.last_reboot_at = timezone.now()

    tel.save()

    if reboot_detected:
        StationAuditLog.objects.create(
            station=station,
            event_type=StationAuditLog.EventType.REBOOT,
            message=f"Station rebooted (reason: {tel.last_reboot_reason or 'unknown'})",
            changes={"boot_id": {"old": prev_boot_id, "new": new_boot_id}},
        )
    return tel
```

> Note: confirm the `StationAuditLog.objects.create(...)` kwargs match the model's
> actual fields (the exploration showed `event_type`, `message`, `changes`,
> `station`). If `message` is named differently, adapt to the real field name.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_telemetry_ingest.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/stations/ingest.py tests/test_telemetry_ingest.py
git commit -m "feat(stations): ingest_telemetry upsert + reboot-transition audit"
```

---

## Task 10: Server — wire `ingest_telemetry` into `HeartbeatView`

**Files:**
- Modify: `apps/api/views.py` (`HeartbeatView`)
- Test: `tests/test_heartbeat_telemetry_e2e.py`

**Interfaces:**
- Consumes: `ingest_telemetry` (Task 9), existing `HeartbeatView` auth + station resolution.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_heartbeat_telemetry_e2e.py
import json
import pytest
from django.test import Client

from apps.stations.models import StationTelemetry
# Reuse conftest fixtures: station_with_key, device_auth_headers


@pytest.mark.django_db
def test_heartbeat_persists_telemetry(station_with_key, device_auth_headers):
    station, private_key = station_with_key
    body = {
        "hostname": "h", "os_version": "o", "uptime": 5.0,
        "module_versions": {}, "ip_address": "10.0.0.2",
        "telemetry": {"boot": {"boot_id": "x1", "boot_count": 1, "reboot_reason": "clean"},
                      "slot": {"active_slot": "a", "image_version": "v2"}},
    }
    body_bytes = json.dumps(body).encode()
    headers = device_auth_headers(private_key, station.id, body_bytes)
    resp = Client().post("/api/v1/heartbeat/", data=body_bytes,
                         content_type="application/json", **headers)
    assert resp.status_code == 200
    tel = StationTelemetry.objects.get(station=station)
    assert tel.boot_id == "x1"
    assert tel.active_slot == "a"


@pytest.mark.django_db
def test_heartbeat_malformed_telemetry_still_200(station_with_key, device_auth_headers):
    station, private_key = station_with_key
    body = {"hostname": "h", "os_version": "o", "uptime": 5.0,
            "module_versions": {}, "ip_address": "10.0.0.2",
            "telemetry": {"boot": "garbage"}}
    body_bytes = json.dumps(body).encode()
    headers = device_auth_headers(private_key, station.id, body_bytes)
    resp = Client().post("/api/v1/heartbeat/", data=body_bytes,
                         content_type="application/json", **headers)
    assert resp.status_code == 200
```

> Check `tests/conftest.py` for the exact header-helper name/signature
> (`device_auth_headers(private_key, station_id, body_bytes)`) and the
> `station_with_key` fixture shape before running; adapt the call if needed.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_heartbeat_telemetry_e2e.py -q`
Expected: FAIL (`StationTelemetry.DoesNotExist` — not yet wired).

- [ ] **Step 3: Implement**

In `apps/api/views.py`, inside `HeartbeatView.post`, after the existing inventory persistence block and before the WebSocket broadcast, add:

```python
        telemetry = serializer.validated_data.get("telemetry")
        if telemetry:
            from apps.stations.ingest import ingest_telemetry
            try:
                ingest_telemetry(station, telemetry)
            except Exception:  # noqa: BLE001 - telemetry must not break heartbeat
                logger.exception("telemetry ingest failed for station %s", station.pk)
```

Ensure `logger` is defined in the module (it is used elsewhere; if not, add `logger = logging.getLogger(__name__)`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_heartbeat_telemetry_e2e.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole heartbeat test slice**

Run: `python -m pytest tests/test_heartbeat_inventory.py tests/test_heartbeat_telemetry_e2e.py tests/test_heartbeat_telemetry_serializer.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/views.py tests/test_heartbeat_telemetry_e2e.py
git commit -m "feat(api): persist telemetry on heartbeat without risking liveness"
```

---

## Task 11: Server — telemetry section on the station detail page

**Files:**
- Modify: `apps/stations/views.py` (`StationDetailView` — prefetch `telemetry`)
- Modify: `apps/stations/templates/stations/station_detail.html`
- Test: `tests/test_station_detail_telemetry.py`

**Interfaces:**
- Consumes: `station.telemetry` (Task 8/9).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_station_detail_telemetry.py
import pytest
from django.urls import reverse

from apps.stations.models import Station, StationTelemetry
# Reuse a logged-in admin fixture from conftest if present; else create inline.


@pytest.mark.django_db
def test_detail_shows_telemetry(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="admin", password="pw", is_staff=True, is_superuser=True)
    client.force_login(user)
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station, boot_count=7, last_reboot_reason="crash",
        active_slot="b", image_version="v3", worst_pre_eol="warning")
    resp = client.get(reverse("stations:detail", args=[station.pk]))
    content = resp.content.decode()
    assert "crash" in content
    assert "v3" in content
```

> Verify the detail URL name (`stations:detail` vs `stations:station_detail`)
> and the admin/login fixture in `conftest.py`; adapt before running.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_station_detail_telemetry.py -q`
Expected: FAIL (telemetry not rendered).

- [ ] **Step 3: Implement**

In `StationDetailView`, add `"telemetry"` to the `select_related`/`prefetch_related` chain (it is a reverse one-to-one → use `select_related("telemetry")` if the base queryset selects on Station, or guard with `getattr(station, "telemetry", None)` in the template).

In `station_detail.html`, add a telemetry card/tab after the hardware inventory tab. Use `{% comment %}`, never multi-line `{# #}`:

```html
{% comment %} Telemetry: boot/power/slot/storage reported by the agent {% endcomment %}
{% with tel=station.telemetry %}
{% if tel %}
<section class="card mt-3">
  <div class="card-header">Telemetry <small class="text-muted">· updated {{ tel.updated_at|timesince }} ago</small></div>
  <div class="card-body">
    <dl class="row mb-0">
      <dt class="col-sm-3">Boot count</dt><dd class="col-sm-3">{{ tel.boot_count }}</dd>
      <dt class="col-sm-3">Uptime</dt><dd class="col-sm-3">{{ tel.uptime_seconds|default:"—" }}</dd>
      <dt class="col-sm-3">Last reboot</dt>
      <dd class="col-sm-3">
        {% if tel.last_reboot_reason %}
          <span class="badge bg-{% if tel.last_reboot_reason == 'clean' %}success{% elif tel.last_reboot_reason == 'ota_rollback' or tel.last_reboot_reason == 'undervoltage' %}warning{% elif tel.last_reboot_reason == 'unknown' %}secondary{% else %}danger{% endif %}">{{ tel.last_reboot_reason }}</span>
          {% if tel.last_reboot_at %}<small class="text-muted">{{ tel.last_reboot_at|timesince }} ago</small>{% endif %}
        {% else %}—{% endif %}
      </dd>
      <dt class="col-sm-3">Active slot</dt><dd class="col-sm-3">{{ tel.active_slot|default:"—" }}</dd>
      <dt class="col-sm-3">Image version</dt><dd class="col-sm-3">{{ tel.image_version|default:"—" }}</dd>
      <dt class="col-sm-3">Last OTA</dt><dd class="col-sm-3">{{ tel.last_ota_result|default:"—" }}</dd>
      <dt class="col-sm-3">Undervoltage</dt>
      <dd class="col-sm-3">{% if tel.undervoltage_now %}<span class="badge bg-danger">now</span>{% elif tel.undervoltage_occurred %}<span class="badge bg-warning">occurred</span>{% else %}ok{% endif %}</dd>
      <dt class="col-sm-3">Storage wear</dt>
      <dd class="col-sm-3">{{ tel.worst_life_time_pct|default:"—" }}{% if tel.worst_life_time_pct %}%{% endif %}
        {% if tel.worst_pre_eol and tel.worst_pre_eol != 'normal' and tel.worst_pre_eol != 'n/a' %}<span class="badge bg-warning">{{ tel.worst_pre_eol }}</span>{% endif %}</dd>
      <dt class="col-sm-3">I/O errors</dt><dd class="col-sm-3">{{ tel.io_error_count }}</dd>
    </dl>
  </div>
</section>
{% endif %}
{% endwith %}
```

Adapt classes to the template's existing Bootstrap/HTMX conventions.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_station_detail_telemetry.py -q`
Expected: PASS.

- [ ] **Step 5: Run the template guard / full stations slice**

Run: `python -m pytest tests/test_station_detail_telemetry.py -q` and the existing template-guard check if runnable locally.
Expected: PASS; no multi-line `{# #}` introduced.

- [ ] **Step 6: Commit**

```bash
git add apps/stations/views.py apps/stations/templates/stations/station_detail.html tests/test_station_detail_telemetry.py
git commit -m "feat(stations): show telemetry on station detail page"
```

---

## Task 12: Monitoring — new alert types + default rules

**Files:**
- Modify: `apps/monitoring/models.py` (`AlertType`)
- Modify: `apps/monitoring/management/commands/create_default_alert_rules.py`
- Test: `tests/test_monitoring_new_rules.py`

**Interfaces:**
- Produces: `AlertType.UNEXPECTED_REBOOT`, `AlertType.POWER_WARNING`, `AlertType.STORAGE_HEALTH`; default `AlertRule` rows for each.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_monitoring_new_rules.py
import pytest
from django.core.management import call_command

from apps.monitoring.models import AlertType, AlertRule


def test_new_alert_types_exist():
    assert AlertType.UNEXPECTED_REBOOT == "unexpected_reboot"
    assert AlertType.POWER_WARNING == "power_warning"
    assert AlertType.STORAGE_HEALTH == "storage_health"


@pytest.mark.django_db
def test_default_rules_seed_new_types():
    call_command("create_default_alert_rules")
    for t in (AlertType.UNEXPECTED_REBOOT, AlertType.POWER_WARNING,
              AlertType.STORAGE_HEALTH):
        assert AlertRule.objects.filter(alert_type=t).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_monitoring_new_rules.py -q`
Expected: FAIL (new enum members missing).

- [ ] **Step 3: Implement**

In `apps/monitoring/models.py`, add to `AlertType`:

```python
    UNEXPECTED_REBOOT = "unexpected_reboot", _("Unexpected Reboot")
    POWER_WARNING = "power_warning", _("Power Warning")
    STORAGE_HEALTH = "storage_health", _("Storage Health")
```

In `create_default_alert_rules.py`, add entries following the existing structure (match the command's current dict/loop shape). Example thresholds:

```python
        (AlertType.UNEXPECTED_REBOOT, 0, Severity.WARNING, "Unerwarteter Reboot (Crash/Watchdog/Power)"),
        (AlertType.POWER_WARNING, 0, Severity.WARNING, "Undervoltage / Throttling erkannt"),
        (AlertType.STORAGE_HEALTH, 80, Severity.WARNING, "SD/eMMC-Verschleiß oder I/O-Fehler"),
```

Adapt tuple/field names to the command's real signature (check `threshold`/`severity`/`description`/`is_active` field names).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_monitoring_new_rules.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/monitoring/models.py apps/monitoring/management/commands/create_default_alert_rules.py tests/test_monitoring_new_rules.py
git commit -m "feat(monitoring): add unexpected_reboot/power/storage alert types + default rules"
```

---

## Task 13: Monitoring — `_check_unexpected_reboot`

**Files:**
- Modify: `apps/monitoring/engine.py`
- Test: `tests/test_monitoring_reboot.py`

**Interfaces:**
- Consumes: `StationTelemetry.last_reboot_at`/`last_reboot_reason`/`boot_count`, `AlertRule` for `UNEXPECTED_REBOOT`, existing `Alert` creation helpers.
- Produces: `_check_unexpected_reboot() -> list[Alert]`. Fires for stations whose `last_reboot_at` is within the engine window and `last_reboot_reason ∈ {crash, watchdog, undervoltage, unknown}`. **Per-reboot dedup:** skip if an `UNEXPECTED_REBOOT` alert exists for the station with `created_at >= last_reboot_at`. Event alert (no auto-resolve).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_monitoring_reboot.py
import pytest
from datetime import timedelta
from django.utils import timezone

from apps.monitoring.engine import _check_unexpected_reboot
from apps.monitoring.models import Alert, AlertRule, AlertType, Severity
from apps.stations.models import Station, StationTelemetry


@pytest.fixture
def reboot_rule(db):
    return AlertRule.objects.create(
        alert_type=AlertType.UNEXPECTED_REBOOT, threshold=0,
        severity=Severity.WARNING, is_active=True)


@pytest.mark.django_db
def test_crash_reboot_raises_alert(reboot_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station, last_reboot_at=timezone.now(),
        last_reboot_reason="crash", boot_count=5)
    alerts = _check_unexpected_reboot()
    assert len(alerts) == 1
    assert alerts[0].station == station


@pytest.mark.django_db
def test_clean_reboot_no_alert(reboot_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station, last_reboot_at=timezone.now(), last_reboot_reason="clean")
    assert _check_unexpected_reboot() == []


@pytest.mark.django_db
def test_same_reboot_dedup(reboot_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station, last_reboot_at=timezone.now(), last_reboot_reason="crash")
    first = _check_unexpected_reboot()
    assert len(first) == 1
    assert _check_unexpected_reboot() == []  # same reboot, no dupe


@pytest.mark.django_db
def test_second_distinct_reboot_realerts(reboot_rule):
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station, last_reboot_at=timezone.now() - timedelta(minutes=1),
        last_reboot_reason="crash")
    _check_unexpected_reboot()
    # A new reboot happens (newer last_reboot_at), first alert still unresolved.
    tel.last_reboot_at = timezone.now()
    tel.save(update_fields=["last_reboot_at"])
    second = _check_unexpected_reboot()
    assert len(second) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_monitoring_reboot.py -q`
Expected: FAIL (`cannot import name _check_unexpected_reboot`).

- [ ] **Step 3: Implement**

In `apps/monitoring/engine.py` (follow the file's existing helpers for window
constant, rule lookup, and alert creation — reuse `_get_active_rule`/`Alert`
creation patterns already present):

```python
UNEXPECTED_REBOOT_REASONS = {"crash", "watchdog", "undervoltage", "unknown"}


def _check_unexpected_reboot():
    """Alert on reboots whose reason is not clean/ota_rollback (event alert)."""
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertType.UNEXPECTED_REBOOT)
    if rule is None:
        return []

    window_start = timezone.now() - CHECK_WINDOW  # reuse the engine's window const
    new_alerts = []
    qs = StationTelemetry.objects.select_related("station").filter(
        last_reboot_at__isnull=False,
        last_reboot_at__gte=window_start,
        last_reboot_reason__in=UNEXPECTED_REBOOT_REASONS,
    )
    for tel in qs:
        already = Alert.objects.filter(
            station=tel.station,
            alert_rule__alert_type=AlertType.UNEXPECTED_REBOOT,
            created_at__gte=tel.last_reboot_at,
        ).exists()
        if already:
            continue
        alert = Alert.objects.create(
            station=tel.station, alert_rule=rule, severity=rule.severity,
            title=f"Unexpected reboot: {tel.last_reboot_reason}",
            message=(f"Station {tel.station.name} rebooted unexpectedly "
                     f"(reason: {tel.last_reboot_reason}, boot #{tel.boot_count})."),
        )
        new_alerts.append(alert)
    return new_alerts
```

> Adapt `_get_active_rule`, `CHECK_WINDOW`, and `Alert.objects.create` kwargs to
> the exact names in `engine.py`. If the engine uses a different window per check,
> define a small module constant (e.g. `REBOOT_CHECK_WINDOW = timedelta(minutes=10)`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_monitoring_reboot.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/monitoring/engine.py tests/test_monitoring_reboot.py
git commit -m "feat(monitoring): unexpected-reboot check with per-reboot dedup"
```

---

## Task 14: Monitoring — `_check_power_warning`

**Files:**
- Modify: `apps/monitoring/engine.py`
- Test: `tests/test_monitoring_power.py`

**Interfaces:**
- Produces: `_check_power_warning() -> list[Alert]`. Fires when `undervoltage_occurred` or `throttled_occurred`. Severity `critical` if a `*_now` bit is set, else `warning`. Auto-resolves when both `*_now` are false (reuse the engine's auto-resolve helper).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_monitoring_power.py
import pytest
from django.utils import timezone

from apps.monitoring.engine import _check_power_warning
from apps.monitoring.models import Alert, AlertRule, AlertType, Severity
from apps.stations.models import Station, StationTelemetry


@pytest.fixture
def power_rule(db):
    return AlertRule.objects.create(
        alert_type=AlertType.POWER_WARNING, threshold=0,
        severity=Severity.WARNING, is_active=True)


@pytest.mark.django_db
def test_undervoltage_now_is_critical(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station, undervoltage_now=True, undervoltage_occurred=True)
    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert alerts[0].severity == Severity.CRITICAL


@pytest.mark.django_db
def test_occurred_only_is_warning(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station, undervoltage_now=False, undervoltage_occurred=True)
    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert alerts[0].severity == Severity.WARNING


@pytest.mark.django_db
def test_no_power_issue_no_alert(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station, undervoltage_occurred=False, throttled_occurred=False)
    assert _check_power_warning() == []


@pytest.mark.django_db
def test_power_dedup(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(station=station, undervoltage_occurred=True)
    assert len(_check_power_warning()) == 1
    assert _check_power_warning() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_monitoring_power.py -q`
Expected: FAIL (`cannot import name _check_power_warning`).

- [ ] **Step 3: Implement**

```python
def _check_power_warning():
    """Alert on undervoltage/throttling (critical if happening now)."""
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertType.POWER_WARNING)
    if rule is None:
        return []

    new_alerts = []
    qs = StationTelemetry.objects.select_related("station").filter(
        Q(undervoltage_occurred=True) | Q(throttled_occurred=True)
    )
    for tel in qs:
        is_now = bool(tel.undervoltage_now) or bool(tel.throttled_now)
        severity = Severity.CRITICAL if is_now else Severity.WARNING
        if _has_unresolved_alert(tel.station, AlertType.POWER_WARNING):
            continue
        what = "Undervoltage" if tel.undervoltage_occurred else "Throttling"
        alert = Alert.objects.create(
            station=tel.station, alert_rule=rule, severity=severity,
            title=f"Power warning: {what}",
            message=(f"Station {tel.station.name}: {what} "
                     f"{'ongoing' if is_now else 'occurred'}."),
        )
        new_alerts.append(alert)

    # Auto-resolve power alerts once nothing is happening now and nothing occurred.
    _auto_resolve_cleared(
        AlertType.POWER_WARNING,
        cleared_station_ids=StationTelemetry.objects.filter(
            undervoltage_now=False, throttled_now=False,
            undervoltage_occurred=False, throttled_occurred=False,
        ).values_list("station_id", flat=True),
    )
    return new_alerts
```

> Reuse `_has_unresolved_alert` (exists) and whatever auto-resolve helper the
> engine already provides; if there is no generic `_auto_resolve_cleared`, follow
> the exact auto-resolve code used by `_check_station_offline`/`_check_cpu_temperature`
> and inline the equivalent for the cleared station set. Import `Q` from
> `django.db.models` if not already imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_monitoring_power.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/monitoring/engine.py tests/test_monitoring_power.py
git commit -m "feat(monitoring): power/undervoltage/throttle check"
```

---

## Task 15: Monitoring — `_check_storage_health` + wire all three into `check_alerts`

**Files:**
- Modify: `apps/monitoring/engine.py` (add `_check_storage_health`; register the three new checks in `check_alerts()`)
- Test: `tests/test_monitoring_storage.py`, `tests/test_monitoring_check_alerts_wiring.py`

**Interfaces:**
- Consumes: `StationTelemetry.worst_pre_eol`/`worst_life_time_pct`/`io_error_count`.
- Produces: `_check_storage_health() -> list[Alert]`; `check_alerts()` now also calls `_check_unexpected_reboot`, `_check_power_warning`, `_check_storage_health`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_monitoring_storage.py
import pytest

from apps.monitoring.engine import _check_storage_health
from apps.monitoring.models import AlertRule, AlertType, Severity
from apps.stations.models import Station, StationTelemetry


@pytest.fixture
def storage_rule(db):
    return AlertRule.objects.create(
        alert_type=AlertType.STORAGE_HEALTH, threshold=80,
        severity=Severity.WARNING, is_active=True)


@pytest.mark.django_db
def test_pre_eol_urgent_is_critical(storage_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(station=station, worst_pre_eol="urgent")
    alerts = _check_storage_health()
    assert len(alerts) == 1
    assert alerts[0].severity == Severity.CRITICAL


@pytest.mark.django_db
def test_high_wear_over_threshold(storage_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(station=station, worst_life_time_pct=90)
    assert len(_check_storage_health()) == 1


@pytest.mark.django_db
def test_io_errors_raise(storage_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(station=station, io_error_count=3)
    assert len(_check_storage_health()) == 1


@pytest.mark.django_db
def test_healthy_storage_no_alert(storage_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station, worst_pre_eol="normal", worst_life_time_pct=10,
        io_error_count=0)
    assert _check_storage_health() == []
```

```python
# tests/test_monitoring_check_alerts_wiring.py
from unittest import mock
from apps.monitoring import engine


def test_check_alerts_calls_new_checks():
    with mock.patch.object(engine, "_check_unexpected_reboot", return_value=[]) as r, \
         mock.patch.object(engine, "_check_power_warning", return_value=[]) as p, \
         mock.patch.object(engine, "_check_storage_health", return_value=[]) as s, \
         mock.patch.object(engine, "_check_station_offline", return_value=[]), \
         mock.patch.object(engine, "_check_cpu_temperature", return_value=[]), \
         mock.patch.object(engine, "_check_disk_usage", return_value=[]), \
         mock.patch.object(engine, "_check_ram_usage", return_value=[]), \
         mock.patch.object(engine, "_check_ota_failed", return_value=[]):
        engine.check_alerts()
    r.assert_called_once()
    p.assert_called_once()
    s.assert_called_once()
```

> Adjust the mocked existing-check names to match `engine.py` exactly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_monitoring_storage.py tests/test_monitoring_check_alerts_wiring.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
def _check_storage_health():
    """Alert on eMMC/SD wear (pre-eol / life-time) or I/O errors."""
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertType.STORAGE_HEALTH)
    if rule is None:
        return []

    new_alerts = []
    for tel in StationTelemetry.objects.select_related("station"):
        urgent = tel.worst_pre_eol == "urgent"
        warning = tel.worst_pre_eol == "warning"
        high_wear = (tel.worst_life_time_pct or 0) >= rule.threshold
        io_errors = tel.io_error_count > 0
        if not (urgent or warning or high_wear or io_errors):
            continue
        if _has_unresolved_alert(tel.station, AlertType.STORAGE_HEALTH):
            continue
        severity = Severity.CRITICAL if (urgent or high_wear) else Severity.WARNING
        reasons = []
        if urgent or warning:
            reasons.append(f"PRE_EOL={tel.worst_pre_eol}")
        if high_wear:
            reasons.append(f"life={tel.worst_life_time_pct}%")
        if io_errors:
            reasons.append(f"{tel.io_error_count} I/O errors")
        alert = Alert.objects.create(
            station=tel.station, alert_rule=rule, severity=severity,
            title="Storage health warning",
            message=f"Station {tel.station.name}: {', '.join(reasons)}.",
        )
        new_alerts.append(alert)
    return new_alerts
```

In `check_alerts()`, add the three calls where the other checks are aggregated:

```python
    new_alerts.extend(_check_unexpected_reboot())
    new_alerts.extend(_check_power_warning())
    new_alerts.extend(_check_storage_health())
```

(Match the exact aggregation style `check_alerts` already uses.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_monitoring_storage.py tests/test_monitoring_check_alerts_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/monitoring/engine.py tests/test_monitoring_storage.py tests/test_monitoring_check_alerts_wiring.py
git commit -m "feat(monitoring): storage-health check + wire new checks into check_alerts"
```

---

## Task 16: Notification routing smoke test for new alert types

**Files:**
- Test: `tests/test_notification_new_alert_types.py`

**Interfaces:**
- Consumes: `send_alert_notifications` (existing), topology recipients (existing), new `AlertType`s.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notification_new_alert_types.py
import pytest
from django.core import mail

from apps.monitoring.models import Alert, AlertRule, AlertType, Severity
from apps.monitoring.notifications import send_alert_notifications
from apps.stations.models import Station, StationAssignment

# Reuse user/topology fixtures from tests/test_notification_dispatch.py patterns.


@pytest.mark.django_db
def test_unexpected_reboot_alert_emails_station_admin(settings, django_user_model):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    admin = django_user_model.objects.create_user(
        username="sa", email="sa@x", password="pw",
        membership_level=django_user_model.MembershipLevel.MEMBER,
        notify_channel=django_user_model.NotifyChannel.EMAIL)
    station = Station.objects.create(name="OE5A")
    StationAssignment.objects.create(user=admin, station=station, role="admin")
    rule = AlertRule.objects.create(
        alert_type=AlertType.UNEXPECTED_REBOOT, threshold=0,
        severity=Severity.WARNING, is_active=True)
    alert = Alert.objects.create(
        station=station, alert_rule=rule, severity=Severity.WARNING,
        title="Unexpected reboot: crash", message="boom")
    send_alert_notifications(alert)
    assert any("sa@x" in m.to for m in mail.outbox)
```

> Align the `membership_level`/`NotifyChannel`/`StationAssignment.role` values and
> the user-creation kwargs with `tests/test_notification_dispatch.py`.

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/test_notification_new_alert_types.py -q`
Expected: If it already passes, the routing is correctly channel-agnostic (good — the new types reuse the existing dispatch). If it fails, fix the dispatch/recipient path so new types route like existing ones.

- [ ] **Step 3: Commit**

```bash
git add tests/test_notification_new_alert_types.py
git commit -m "test(monitoring): new alert types route via existing topology channels"
```

---

## Final verification (before PR)

- [ ] Run the full agent + server + monitoring test suites:

```bash
python -m pytest -q
```

Expected: all green.

- [ ] Check migrations are complete and consistent:

```bash
python manage.py makemigrations --check --dry-run
```

Expected: "No changes detected".

- [ ] Grep for forbidden multi-line template comments in touched templates:

```bash
grep -n "{#" apps/stations/templates/stations/station_detail.html || echo "clean"
```

- [ ] Update `feature/station-telemetry-alerting/progress` memory, open PR → `main`, request Copilot review, run copilot-loop to 0.
