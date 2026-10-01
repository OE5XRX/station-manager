# station_agent/bootinfo.py
"""Boot / reboot-reason detection — agent-owned per telemetry contract."""

import json
import logging
import os
import subprocess

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
    from station_agent import power
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
