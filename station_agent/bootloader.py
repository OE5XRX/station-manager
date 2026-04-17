"""Bootloader abstraction for A/B boot management.

Supports U-Boot (fw_setenv/fw_printenv) and GRUB (grub-editenv).
The active backend is chosen via config.yml's 'bootloader' field.
"""

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

GRUB_ENV_PATH = "/boot/EFI/BOOT/grubenv"
UBOOT_ENV_TOOL = "fw_setenv"
UBOOT_PRINT_TOOL = "fw_printenv"
GRUB_ENV_TOOL = "grub-editenv"


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


def get_active_slot(bootloader: str) -> str:
    """Return the currently active boot slot ('a' or 'b')."""
    slot = get_env(bootloader, "boot_part")
    return slot if slot in ("a", "b") else "a"


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
