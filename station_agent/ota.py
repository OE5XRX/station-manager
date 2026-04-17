"""OTA update client for the Station Agent.

Handles checking for updates, downloading firmware, reporting status,
and committing successful boots. Actual partition writing is deferred
to Yocto integration.
"""

import hashlib
import logging
import os
import subprocess

from .bootloader import commit_boot_local, get_bootloader, get_inactive_slot, set_upgrade_pending
from .http_client import HttpClient

logger = logging.getLogger(__name__)


def check_for_update(config, http_client: HttpClient) -> dict | None:
    """Check the server for a pending deployment.

    Args:
        config: AgentConfig instance.
        http_client: Authenticated HTTP client.

    Returns:
        Deployment info dict if an update is available, None otherwise.
    """
    response = http_client.request("GET", "/api/v1/deployments/check/")
    if response is None:
        return None

    if response.status_code == 200:
        try:
            data = response.json()
            logger.info("Update available: deployment %s", data.get("id", "?"))
            return data
        except ValueError:
            logger.error("Invalid JSON in deployment check response")
            return None
    elif response.status_code == 204:
        logger.debug("No update available")
        return None
    else:
        logger.warning(
            "Unexpected status from deployment check: %s %s",
            response.status_code,
            response.text[:200],
        )
        return None


def download_firmware(
    config,
    http_client: HttpClient,
    download_url: str,
    expected_checksum: str,
    dest_path: str,
) -> bool:
    """Download firmware and verify its SHA-256 checksum.

    Args:
        config: AgentConfig instance.
        http_client: Authenticated HTTP client.
        download_url: API path to download the firmware from.
        expected_checksum: Expected SHA-256 hex digest.
        dest_path: Local filesystem path to write the firmware file.

    Returns:
        True if download and checksum verification succeeded.
    """
    # Ensure download directory exists
    dest_dir = os.path.dirname(dest_path)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create download directory %s: %s", dest_dir, exc)
        return False

    response = http_client.request("GET", download_url, stream=True)
    if response is None:
        return False

    if response.status_code != 200:
        logger.error(
            "Firmware download failed: %s %s",
            response.status_code,
            response.text[:200],
        )
        return False

    sha256 = hashlib.sha256()
    try:
        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    sha256.update(chunk)
    except OSError as exc:
        logger.error("Failed to write firmware to %s: %s", dest_path, exc)
        return False

    actual_checksum = sha256.hexdigest()
    if actual_checksum != expected_checksum:
        logger.error("Checksum mismatch: expected %s, got %s", expected_checksum, actual_checksum)
        # Remove corrupt download
        try:
            os.remove(dest_path)
        except OSError:
            pass
        return False

    logger.info("Firmware downloaded and verified: %s", dest_path)
    return True


def report_status(
    config,
    http_client: HttpClient,
    result_pk: int,
    status: str,
    error_message: str = "",
) -> bool:
    """Report deployment status to the server.

    Args:
        config: AgentConfig instance.
        http_client: Authenticated HTTP client.
        result_pk: Primary key of the deployment result.
        status: One of: downloading, installing, rebooting, verifying,
                failed, rolled_back.
        error_message: Optional error description for failure states.

    Returns:
        True if the status was reported successfully.
    """
    path = f"/api/v1/deployments/{result_pk}/status/"
    payload = {"status": status, "error_message": error_message}

    response = http_client.request("POST", path, json_data=payload)
    if response is None:
        return False

    if response.status_code == 200:
        logger.info("Reported deployment %s status: %s", result_pk, status)
        return True
    else:
        logger.warning(
            "Status report rejected: %s %s",
            response.status_code,
            response.text[:200],
        )
        return False


def commit_boot(config, http_client: HttpClient, version: str) -> bool:
    """Commit the current boot as successful.

    Two-phase commit:
    1. Tell the bootloader that this boot is good (reset bootcount).
    2. Tell the server so the deployment is marked as completed.

    Args:
        config: AgentConfig instance.
        http_client: Authenticated HTTP client.
        version: Firmware version string to commit.

    Returns:
        True if both local and server commit succeeded.
    """
    bl = get_bootloader(config)
    if not commit_boot_local(bl):
        logger.error("Local bootloader commit failed — NOT reporting to server")
        return False

    payload = {"version": version}
    response = http_client.request("POST", "/api/v1/deployments/commit/", json_data=payload)
    if response is None:
        return False

    if response.status_code == 200:
        logger.info("Boot committed for version %s", version)
        return True
    else:
        logger.warning(
            "Boot commit rejected: %s %s",
            response.status_code,
            response.text[:200],
        )
        return False


def apply_update(config, firmware_path: str) -> bool:
    """Apply a firmware update to the inactive partition.

    Writes the firmware image to the inactive root partition via dd,
    then sets the bootloader to trial-boot from it on next reboot.

    Args:
        config: AgentConfig instance.
        firmware_path: Path to the downloaded firmware file.

    Returns:
        True if the update was applied successfully.
    """
    bl = get_bootloader(config)
    target_slot = get_inactive_slot(bl)
    target_dev = f"/dev/disk/by-partlabel/root_{target_slot}"

    if not os.path.exists(target_dev):
        logger.error("Inactive partition not found: %s", target_dev)
        return False

    logger.info("Writing %s to %s (slot %s)", firmware_path, target_dev, target_slot)
    try:
        subprocess.run(
            ["dd", f"if={firmware_path}", f"of={target_dev}", "bs=4M", "conv=fsync"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("dd failed (rc=%d): %s", exc.returncode, exc.stderr.strip())
        return False

    if not set_upgrade_pending(bl, target_slot):
        logger.error("Failed to set bootloader to trial-boot slot %s", target_slot)
        return False

    logger.info("Update applied to slot %s — reboot to activate", target_slot)
    return True
