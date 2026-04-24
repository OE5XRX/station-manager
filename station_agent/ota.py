"""OTA update client for the Station Agent.

Handles checking for updates, downloading firmware (resumable, opaque
URL), reporting status, stream-decompressing the bz2-compressed rootfs
into the inactive rootfs partition (install_to_slot), arming the
bootloader for a trial boot, and committing successful boots.
"""

import bz2
import hashlib
import logging
import os
import re

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
    expected_size: int | None = None,
) -> bool:
    """Download a firmware image, optionally resuming from a partial file.

    download_url is used verbatim — callers must not parse or rebuild it.

    If ``expected_size`` is provided and a local partial already matches
    it, skip the network round-trip entirely and only verify the
    checksum. A partial larger than ``expected_size`` is treated as
    stale and discarded before falling through to a fresh download.
    """
    headers: dict[str, str] = {}
    existing_len = 0
    mode = "wb"
    if resume and os.path.exists(dest_path):
        existing_len = os.path.getsize(dest_path)
        if expected_size is not None and existing_len >= expected_size:
            if existing_len == expected_size:
                # File already fully downloaded (e.g. the previous run
                # wrote it, reported DOWNLOADING, then died before it
                # could advance to INSTALLING). Skip the HTTP request
                # and fall straight through to checksum verification.
                logger.info(
                    "Partial at %s matches expected size %d — skipping download",
                    dest_path,
                    expected_size,
                )
                return _verify_checksum(dest_path, expected_checksum)
            # existing_len > expected_size: partial is stale (probably
            # left from a larger previous release). Drop it and do a
            # full download instead of sending a Range that the server
            # would 416 anyway.
            logger.info(
                "Partial at %s is larger than expected (%d > %d); discarding",
                dest_path,
                existing_len,
                expected_size,
            )
            try:
                os.remove(dest_path)
            except OSError:
                pass
            existing_len = 0
        if existing_len > 0:
            headers["Range"] = f"bytes={existing_len}-"
            mode = "ab"

    resp = http_client.request("GET", download_url, stream=True, headers=headers)
    if resp is None:
        return False

    if resp.status_code == 416 and existing_len > 0:
        # Our partial file is stale (e.g. left over from a different
        # release, or the server decided Range isn't servable). Drop it
        # and restart from zero without Range — one retry, no loop.
        try:
            resp.close()
        except Exception as exc:
            logger.debug("Response close failed (ignored): %s", exc)
        try:
            os.remove(dest_path)
        except OSError:
            pass
        logger.info("Server rejected Range (416); restarting download from 0")
        return download_firmware_resumable(
            http_client=http_client,
            download_url=download_url,
            expected_checksum=expected_checksum,
            dest_path=dest_path,
            resume=False,
        )

    if resp.status_code == 200:
        # Server refused the Range request — restart from zero.
        mode = "wb"
        existing_len = 0
    elif resp.status_code == 206:
        # A misbehaving proxy may return 206 with a different start
        # offset than we asked for. If we trust it and append, the
        # local file becomes garbage and the SHA-256 check fails at
        # the end — terminal FAILED, no retry. Validate Content-Range
        # against existing_len; on mismatch, drop the partial and
        # restart from 0 in a single recursive call.
        content_range = getattr(resp, "headers", {}).get("Content-Range", "")
        m = re.match(r"bytes\s+(\d+)-", content_range)
        reported_start = int(m.group(1)) if m else None
        if existing_len > 0 and reported_start != existing_len:
            try:
                resp.close()
            except Exception as exc:
                logger.debug("Response close failed (ignored): %s", exc)
            try:
                os.remove(dest_path)
            except OSError:
                pass
            logger.warning(
                "Content-Range start=%r != expected %d; restarting from 0",
                reported_start,
                existing_len,
            )
            return download_firmware_resumable(
                http_client=http_client,
                download_url=download_url,
                expected_checksum=expected_checksum,
                dest_path=dest_path,
                resume=False,
            )
    else:
        logger.error("Firmware download failed: %s", resp.status_code)
        return False

    # The agent's default download_dir (typically /tmp/station-agent)
    # may not exist on first OTA attempt. Create it every call — cheap
    # and makes the no-partial and resume paths behave identically.
    # On permission / read-only FS / full-disk errors, fail cleanly so
    # the caller reports FAILED instead of the agent thread dying.
    parent = os.path.dirname(dest_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create download dir %s: %s", parent, exc)
            try:
                resp.close()
            except Exception:
                pass
            return False

    try:
        try:
            with open(dest_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=_STREAM_CHUNK):
                    if chunk:
                        f.write(chunk)
        except OSError as exc:
            # Full disk, permission error, read-only FS, etc. Must return
            # cleanly so the agent reports FAILED and tries again next
            # poll cycle — letting this propagate would crash the OTA
            # worker thread.
            logger.error("Failed to write firmware to %s: %s", dest_path, exc)
            try:
                os.remove(dest_path)
            except OSError:
                pass
            return False
    finally:
        try:
            resp.close()
        except Exception as exc:
            logger.debug("Response close failed (ignored): %s", exc)

    return _verify_checksum(dest_path, expected_checksum)


def _verify_checksum(dest_path: str, expected_checksum: str) -> bool:
    """Verify ``dest_path`` against ``expected_checksum``.

    An empty ``expected_checksum`` means the caller opted out — return
    True without reading the file. Any filesystem error during the
    read drops the file and returns False so the agent reports FAILED
    cleanly instead of crashing.
    """
    if not expected_checksum:
        return True
    h = hashlib.sha256()
    try:
        with open(dest_path, "rb") as f:
            while True:
                chunk = f.read(_STREAM_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as exc:
        # Disk ejected / file cleared / permissions revoked between
        # the write and the checksum pass.
        logger.error("Failed to read firmware for checksum %s: %s", dest_path, exc)
        try:
            os.remove(dest_path)
        except OSError:
            pass
        return False
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

    Stream-decompresses the downloaded `.rootfs.bz2` into the inactive
    root partition, then arms the bootloader for a trial boot.

    Args:
        config: AgentConfig instance.
        firmware_path: Path to the downloaded .rootfs.bz2 file (a bz2-compressed
            ext4 rootfs image).

    Returns:
        True if the update was applied successfully; False on
        survivable failures (inactive-partition device missing,
        bz2 stream corrupt, bootloader set_upgrade_pending failed).

    Raises:
        RuntimeError: when slot detection fails (both /proc/cmdline
            and root-mount probes unresolved). Callers are expected
            to catch this and report FAILED with the exception text
            — see agent._handle_ota.
    """
    bl = get_bootloader(config)
    # Let RuntimeError from get_active_slot/get_inactive_slot propagate.
    # agent._handle_ota catches it and forwards the message into the
    # server-visible error_message — otherwise the operator sees only
    # the generic "Failed to write firmware..." for what's really a
    # slot-detection failure, and has to ssh in to find the real
    # reason in journalctl.
    target_slot = get_inactive_slot(bl)
    target_dev = f"/dev/disk/by-partlabel/root_{target_slot}"

    if not os.path.exists(target_dev):
        logger.error("Inactive partition not found: %s", target_dev)
        return False

    logger.info("Writing %s to %s (slot %s)", firmware_path, target_dev, target_slot)
    try:
        install_to_slot(firmware_path, target_dev)
    except (OSError, ValueError) as exc:
        logger.error("install_to_slot failed: %s", exc)
        return False

    if not set_upgrade_pending(bl, target_slot):
        logger.error("Failed to set bootloader to trial-boot slot %s", target_slot)
        return False

    logger.info("Update applied to slot %s — reboot to activate", target_slot)
    return True


def install_to_slot(wic_bz2_path, partition_device: str) -> None:
    """Stream-decompress a .rootfs.bz2 into a block device.

    `partition_device` is e.g. "/dev/sda4". The caller is responsible
    for making sure this is the inactive slot — typically derived via
    bootloader.get_inactive_slot() + a machine-specific slot→device map.

    Uses ``bz2.open()`` rather than a manual ``BZ2Decompressor`` loop so
    multi-stream bz2 files (e.g. produced by ``pbzip2`` or Yocto's
    parallel compression paths) decompress correctly — the old manual
    loop raised ``EOFError: End of stream already reached`` as soon as
    it tried to feed more bytes into the decompressor after the first
    stream ended. ``BZ2File`` handles both multi-stream and trailing
    padding the same way the ``bunzip2`` CLI does.

    Raises OSError on I/O failure.
    Raises ValueError if the bz2 stream is truncated or corrupt —
    writing a partial image to a boot slot would silently brick the
    next boot, so fail loud.
    """
    fd = os.open(partition_device, os.O_WRONLY | os.O_SYNC)
    try:
        with bz2.open(str(wic_bz2_path), "rb") as src:
            while True:
                # Narrow the translation to the decompression read only.
                # BZ2File signals truncation as EOFError and a corrupt
                # data stream as OSError *without* errno (e.g. "Invalid
                # data stream"). A real I/O error on the backing file
                # during read (EIO / ESTALE / EBADF / ...) also surfaces
                # as OSError, but *with* errno set — those must keep
                # propagating as OSError so the documented "Raises
                # OSError on I/O failure" contract still holds. Only
                # the former two classes mean "bad firmware artifact"
                # and get translated to ValueError.
                try:
                    chunk = _stream_read(src, _STREAM_CHUNK)
                except EOFError as exc:
                    raise ValueError(f"bz2 stream in {wic_bz2_path} is truncated: {exc}") from exc
                except OSError as exc:
                    if exc.errno is not None:
                        raise
                    raise ValueError(f"bz2 stream in {wic_bz2_path} is corrupt: {exc}") from exc
                if not chunk:
                    break
                _write_all(fd, chunk)
        os.fsync(fd)
    finally:
        os.close(fd)
