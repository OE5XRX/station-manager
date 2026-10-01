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
