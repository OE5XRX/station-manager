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
