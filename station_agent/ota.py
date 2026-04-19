"""OTA update client for the Station Agent.

Handles checking for updates, downloading firmware, reporting status,
and committing successful boots. Actual partition writing is deferred
to Yocto integration.
"""

import bz2
import hashlib
import logging
import os
import subprocess

from .bootloader import commit_boot_local, get_bootloader, get_inactive_slot, set_upgrade_pending
from .http_client import HttpClient
from .inventory import get_current_version

logger = logging.getLogger(__name__)

_STREAM_CHUNK = 1 << 20  # 1 MiB


def _stream_read(fh, n: int) -> bytes:
    # Indirection so tests can count reads.
    return fh.read(n)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def check_for_update(config, http_client: HttpClient) -> dict | None:
    """Check the server for a pending deployment.

    POSTs the current firmware version so the server can decide whether the
    station needs an upgrade. Returns the deployment info dict on 200, or
    None for 204 (no update) / transport failures / unexpected statuses.
    """
    current_version = get_current_version()
    body = {"current_version": current_version}
    response = http_client.request("POST", "/api/v1/deployments/check/", json_data=body)
    if response is None or response.status_code == 204:
        return None
    if response.status_code != 200:
        logger.warning("Unexpected status from deployment check: %s", response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        logger.error("Invalid JSON in deployment check response")
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


def download_firmware_resumable(
    http_client,
    download_url: str,
    expected_checksum: str,
    dest_path: str,
    *,
    resume: bool = True,
) -> bool:
    """Download a firmware image, optionally resuming from a partial file.

    download_url is used verbatim — callers must not parse or rebuild it.
    """
    headers: dict[str, str] = {}
    existing_len = 0
    mode = "wb"
    if resume and os.path.exists(dest_path):
        existing_len = os.path.getsize(dest_path)
        if existing_len > 0:
            headers["Range"] = f"bytes={existing_len}-"
            mode = "ab"

    resp = http_client.request("GET", download_url, stream=True, headers=headers)
    if resp is None:
        return False

    if resp.status_code == 200:
        # Server refused the Range request — restart from zero.
        mode = "wb"
        existing_len = 0
    elif resp.status_code != 206:
        logger.error("Firmware download failed: %s", resp.status_code)
        return False

    try:
        with open(dest_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=_STREAM_CHUNK):
                if chunk:
                    f.write(chunk)
    finally:
        try:
            resp.close()
        except Exception as exc:
            logger.debug("Response close failed (ignored): %s", exc)

    if expected_checksum:
        h = hashlib.sha256()
        with open(dest_path, "rb") as f:
            while True:
                chunk = f.read(_STREAM_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        if h.hexdigest() != expected_checksum:
            logger.error(
                "Checksum mismatch: expected %s got %s",
                expected_checksum,
                h.hexdigest(),
            )
            try:
                os.remove(dest_path)
            except OSError:
                pass
            return False
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


def install_to_slot(wic_bz2_path, partition_device: str) -> None:
    """Stream-decompress a .wic.bz2 into a block device.

    `partition_device` is e.g. "/dev/sda4". The caller is responsible
    for making sure this is the inactive slot — typically derived via
    bootloader.get_inactive_slot() + a machine-specific slot→device map.

    Raises OSError on I/O failure.
    """
    decomp = bz2.BZ2Decompressor()
    with open(str(wic_bz2_path), "rb") as src:
        fd = os.open(partition_device, os.O_WRONLY | os.O_SYNC)
        try:
            while True:
                chunk = _stream_read(src, _STREAM_CHUNK)
                if not chunk:
                    break
                decompressed = decomp.decompress(chunk)
                if decompressed:
                    _write_all(fd, decompressed)
            os.fsync(fd)
        finally:
            os.close(fd)
