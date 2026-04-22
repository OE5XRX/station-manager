"""Bootloader abstraction for A/B boot management.

Supports U-Boot (fw_setenv/fw_printenv) and GRUB (grub-editenv).
The active backend is chosen via config.yml's 'bootloader' field.

Active-slot detection is intentionally bootloader-agnostic: both
oe5xrx-grub.cfg and boot.cmd emit the same
``root=PARTLABEL=root_${boot_part}`` cmdline anchor, so one regex
over ``/proc/cmdline`` handles both. Env-based detection would
misreport the active slot in every "set_upgrade_pending-but-not-
yet-rebooted" window and risk ``install_to_slot`` overwriting the
live rootfs.
"""

import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

GRUB_ENV_PATH = "/boot/EFI/BOOT/grubenv"
UBOOT_ENV_TOOL = "fw_setenv"
UBOOT_PRINT_TOOL = "fw_printenv"
GRUB_ENV_TOOL = "grub-editenv"

_CMDLINE_PATTERN = re.compile(r"root=PARTLABEL=root_([ab])\b")


def _run(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        return False
    except subprocess.CalledProcessError as exc:
        logger.error("%s failed (rc=%d): %s", cmd[0], exc.returncode, exc.stderr.strip())
        return False


def _detect_bootloader() -> str:
    if shutil.which(GRUB_ENV_TOOL):
        return "grub"
    if shutil.which(UBOOT_ENV_TOOL):
        return "uboot"
    return "none"


def _slot_from_cmdline() -> str | None:
    """Primary probe: parse the A/B slot from /proc/cmdline.

    Works for both grub (oe5xrx-grub.cfg) and u-boot (boot.cmd)
    because the wic recipe labels partitions uniformly and both boot
    scripts set ``root=PARTLABEL=root_${boot_part}`` in the kernel
    cmdline. Returns None if /proc/cmdline can't be read or doesn't
    contain a PARTLABEL anchor (older images may boot via
    ``root=/dev/sda2``; the caller falls back to mount inspection).
    """
    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read()
    except OSError:
        return None
    m = _CMDLINE_PATTERN.search(cmdline)
    return m.group(1) if m else None


def _slot_from_root_mount() -> str | None:
    """Fallback probe: compare ``os.stat('/').st_dev`` to the
    device nodes behind the partlabel symlinks. Handles the edge
    case where the cmdline anchor got stripped (initramfs
    rewrite, kernel args injected by a service).
    """
    try:
        root_dev = os.stat("/").st_dev
    except OSError:
        return None
    for slot in ("a", "b"):
        try:
            part = os.stat(f"/dev/disk/by-partlabel/root_{slot}")
        except OSError:
            continue
        if part.st_rdev == root_dev:
            return slot
    return None


def get_env(bootloader: str, key: str) -> str | None:
    """Read a single bootloader env variable."""
    if bootloader == "grub":
        result = subprocess.run(
            [GRUB_ENV_TOOL, GRUB_ENV_PATH, "list"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        return None

    elif bootloader == "uboot":
        result = subprocess.run(
            [UBOOT_PRINT_TOOL, key],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        return None

    return None


def get_active_slot(bootloader: str = "") -> str:
    """Return the currently-running A/B slot.

    Derived from the running rootfs, NOT from the bootloader env.
    The env's ``boot_part`` is a "next-boot hint" that
    ``set_upgrade_pending`` mutates before reboot; reading it would
    misidentify the active slot in every "set_upgrade_pending-but-
    not-yet-rebooted" window and let ``install_to_slot`` overwrite
    the running rootfs.

    Tries ``/proc/cmdline`` first, then the root-mount / partlabel
    comparison. Raises ``RuntimeError`` if neither resolves — fail
    closed, because the alternative (guessing a default slot)
    risks corrupting the running system.

    The ``bootloader`` argument is kept for API compatibility but
    ignored — detection is bootloader-agnostic.
    """
    for probe in (_slot_from_cmdline, _slot_from_root_mount):
        slot = probe()
        if slot in ("a", "b"):
            return slot
    raise RuntimeError(
        "Cannot determine active A/B slot from /proc/cmdline or / mount. "
        "refusing to guess — next install_to_slot could corrupt the running rootfs."
    )


def get_inactive_slot(bootloader: str) -> str:
    """Return the inactive boot slot."""
    return "b" if get_active_slot(bootloader) == "a" else "a"


def commit_boot_local(bootloader: str) -> bool:
    """Mark the current boot as successful (reset bootcount, clear trial)."""
    logger.info("Committing boot on %s bootloader", bootloader)

    if bootloader == "grub":
        return _run([GRUB_ENV_TOOL, GRUB_ENV_PATH, "set", "bootcount=0"]) and _run(
            [GRUB_ENV_TOOL, GRUB_ENV_PATH, "set", "upgrade_available=0"]
        )

    elif bootloader == "uboot":
        return _run([UBOOT_ENV_TOOL, "bootcount", "0", "upgrade_available", "0"])

    else:
        logger.warning("No bootloader configured — skipping local boot commit")
        return True


def set_upgrade_pending(bootloader: str, target_slot: str) -> bool:
    """Set the bootloader to boot from target_slot on next reboot (trial mode)."""
    logger.info("Setting upgrade pending: slot=%s on %s", target_slot, bootloader)

    if target_slot not in ("a", "b"):
        logger.error("Invalid slot: %s", target_slot)
        return False

    if bootloader == "grub":
        return (
            _run([GRUB_ENV_TOOL, GRUB_ENV_PATH, "set", f"boot_part={target_slot}"])
            and _run([GRUB_ENV_TOOL, GRUB_ENV_PATH, "set", "upgrade_available=1"])
            and _run([GRUB_ENV_TOOL, GRUB_ENV_PATH, "set", "bootcount=0"])
        )

    elif bootloader == "uboot":
        return _run(
            [
                UBOOT_ENV_TOOL,
                "boot_part",
                target_slot,
                "upgrade_available",
                "1",
                "bootcount",
                "0",
            ]
        )

    else:
        logger.warning("No bootloader configured — skipping upgrade pending")
        return True


def get_bootloader(config) -> str:
    """Resolve the bootloader backend from config or auto-detect."""
    bl = getattr(config, "bootloader", "auto")
    if bl == "auto":
        bl = _detect_bootloader()
        logger.info("Auto-detected bootloader: %s", bl)
    return bl
