"""OTA update client for the Station Agent.

Handles checking for updates, downloading firmware (resumable, opaque
URL), reporting status, stream-decompressing the bz2-compressed rootfs
into the inactive rootfs partition (install_to_slot), arming the
bootloader for a trial boot, and committing successful boots.
"""

import bz2
import enum
import hashlib
import logging
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass

import requests

from .bootinfo import mark_pending_upgrade
from .bootloader import commit_boot_local, get_bootloader, get_inactive_slot, set_upgrade_pending
from .http_client import HttpClient
from .inventory import get_current_version

logger = logging.getLogger(__name__)

_STREAM_CHUNK = 1 << 20  # 1 MiB
# Flush the page cache to the device every _SYNC_INTERVAL bytes during a
# slot write. Bounds dirty pages so a multi-GB write can't build a huge
# writeback backlog (which spikes I/O and stalls other tasks), and lets us
# drop the now-clean pages (POSIX_FADV_DONTNEED) so a 2 GB image doesn't
# thrash the page cache. See install_to_slot for why we do NOT use O_SYNC.
_SYNC_INTERVAL = 64 << 20  # 64 MiB


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


class DownloadOutcome(enum.Enum):
    """Classification of a firmware download (one attempt or the retry loop)."""

    OK = "ok"
    # Network/transport problem (timeout, dropped connection, 5xx, 408/429,
    # short body). The partial is kept; retrying with Range continues it.
    TRANSIENT = "transient"
    # Retrying cannot help: checksum mismatch, local disk error, 4xx (the
    # deployment is no longer active for us, or the image is gone).
    FATAL = "fatal"
    # Agent shutdown during a backoff wait. Not a failure: the server keeps
    # the result in DOWNLOADING and the next agent run resumes the partial.
    ABORTED = "aborted"


@dataclass(frozen=True)
class DownloadResult:
    outcome: DownloadOutcome
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is DownloadOutcome.OK


# Statuses a proxy / overloaded server / rate limiter returns on a bad
# moment; everything else outside 200/206/416 is treated as final.
_TRANSIENT_HTTP = frozenset({408, 425, 429})

# Per-socket-read timeout for the image download (seconds). The 30 s API
# default is too tight for LTE/HAMNET links that stall for a minute; a stall
# beyond this is just a transient error that resumes via Range.
DOWNLOAD_TIMEOUT = 120

# Backoff between transient download failures: 10 s doubling to 10 min,
# scaled by a jitter factor in [0.5, 1.0) so a fleet that lost the same
# uplink does not hammer the server in lockstep when it comes back.
_BACKOFF_INITIAL_S = 10.0
_BACKOFF_MAX_S = 600.0


def _close_quietly(resp) -> None:
    try:
        resp.close()
    except Exception as exc:
        logger.debug("Response close failed (ignored): %s", exc)


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def download_firmware_resumable(
    http_client,
    download_url: str,
    expected_checksum: str,
    dest_path: str,
    *,
    resume: bool = True,
    expected_size: int | None = None,
) -> bool:
    """Single download attempt as a bool (see ``download_firmware_attempt``)."""
    return download_firmware_attempt(
        http_client=http_client,
        download_url=download_url,
        expected_checksum=expected_checksum,
        dest_path=dest_path,
        resume=resume,
        expected_size=expected_size,
    ).ok


def download_firmware_attempt(
    http_client,
    download_url: str,
    expected_checksum: str,
    dest_path: str,
    *,
    resume: bool = True,
    expected_size: int | None = None,
) -> DownloadResult:
    """One download attempt, optionally resuming from a partial file.

    download_url is used verbatim — callers must not parse or rebuild it.

    If ``expected_size`` is provided and a local partial already matches
    it, skip the network round-trip entirely and only verify the
    checksum. A partial larger than ``expected_size`` is treated as
    stale and discarded before falling through to a fresh download.

    Network errors never delete the partial — they return TRANSIENT so
    ``download_with_retry`` can continue it with a Range request.
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
                return _verified(dest_path, expected_checksum)
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
            _remove_quietly(dest_path)
            existing_len = 0
        if existing_len > 0:
            headers["Range"] = f"bytes={existing_len}-"
            mode = "ab"

    resp = http_client.request(
        "GET", download_url, stream=True, headers=headers, timeout=DOWNLOAD_TIMEOUT
    )
    if resp is None:
        # HttpClient already logged the connect error / timeout.
        return DownloadResult(DownloadOutcome.TRANSIENT, "no response from server")

    if resp.status_code == 416 and existing_len > 0:
        # Our partial file is stale (e.g. left over from a different
        # release, or the server decided Range isn't servable). Drop it
        # and restart from zero without Range — one retry, no loop.
        _close_quietly(resp)
        _remove_quietly(dest_path)
        logger.info("Server rejected Range (416); restarting download from 0")
        return download_firmware_attempt(
            http_client=http_client,
            download_url=download_url,
            expected_checksum=expected_checksum,
            dest_path=dest_path,
            resume=False,
            expected_size=expected_size,
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
            _close_quietly(resp)
            _remove_quietly(dest_path)
            logger.warning(
                "Content-Range start=%r != expected %d; restarting from 0",
                reported_start,
                existing_len,
            )
            return download_firmware_attempt(
                http_client=http_client,
                download_url=download_url,
                expected_checksum=expected_checksum,
                dest_path=dest_path,
                resume=False,
                expected_size=expected_size,
            )
    else:
        code = resp.status_code
        _close_quietly(resp)
        outcome = (
            DownloadOutcome.TRANSIENT
            if code >= 500 or code in _TRANSIENT_HTTP
            else DownloadOutcome.FATAL
        )
        logger.error("Firmware download failed: HTTP %s (%s)", code, outcome.value)
        return DownloadResult(outcome, f"HTTP {code}")

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
            _close_quietly(resp)
            return DownloadResult(DownloadOutcome.FATAL, f"cannot create download dir: {exc}")

    try:
        try:
            with open(dest_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=_STREAM_CHUNK):
                    if chunk:
                        f.write(chunk)
        except requests.RequestException as exc:
            # Connection dropped / read timed out mid-stream. Must come
            # before the OSError branch: requests' exceptions subclass
            # OSError, and that branch deletes the partial we want to
            # resume from.
            logger.warning(
                "Firmware download interrupted at %d bytes: %s", _file_size(dest_path), exc
            )
            return DownloadResult(DownloadOutcome.TRANSIENT, f"connection interrupted: {exc}")
        except OSError as exc:
            # Full disk, permission error, read-only FS, etc. Must return
            # cleanly so the agent reports FAILED and tries again next
            # poll cycle — letting this propagate would crash the OTA
            # worker thread.
            logger.error("Failed to write firmware to %s: %s", dest_path, exc)
            _remove_quietly(dest_path)
            return DownloadResult(DownloadOutcome.FATAL, f"cannot write firmware: {exc}")
    finally:
        _close_quietly(resp)

    if expected_size is not None:
        got = _file_size(dest_path)
        if got < expected_size:
            # The connection ended cleanly but early. Checksumming now
            # would delete a perfectly resumable partial.
            logger.warning("Firmware download ended early (%d of %d bytes)", got, expected_size)
            return DownloadResult(
                DownloadOutcome.TRANSIENT, f"connection closed early ({got}/{expected_size} bytes)"
            )

    return _verified(dest_path, expected_checksum)


def _verified(dest_path: str, expected_checksum: str) -> DownloadResult:
    if _verify_checksum(dest_path, expected_checksum):
        return DownloadResult(DownloadOutcome.OK)
    return DownloadResult(DownloadOutcome.FATAL, "checksum verification failed")


def download_with_retry(
    http_client,
    download_url: str,
    expected_checksum: str,
    dest_path: str,
    *,
    expected_size: int | None = None,
    stall_limit_s: float,
    wait,
    clock=time.monotonic,
    rng=random.random,
    attempt=download_firmware_attempt,
) -> DownloadResult:
    """Download with Range-resume, retrying transient failures with backoff.

    Gives up (returns the last TRANSIENT result) only once the partial has
    not grown for ``stall_limit_s`` seconds — a slow link that keeps making
    progress is never abandoned, and any progress resets the backoff.
    FATAL results return immediately. ``wait(seconds)`` sleeps and returns
    True when the agent is shutting down (``threading.Event.wait``); that
    yields ABORTED so the caller leaves the deployment in DOWNLOADING.
    """
    best_size = _file_size(dest_path)
    last_progress = clock()
    failures = 0
    while True:
        result = attempt(
            http_client=http_client,
            download_url=download_url,
            expected_checksum=expected_checksum,
            dest_path=dest_path,
            expected_size=expected_size,
        )
        if result.outcome is not DownloadOutcome.TRANSIENT:
            return result

        size = _file_size(dest_path)
        if size > best_size:
            best_size = size
            last_progress = clock()
            failures = 0
        # Exponent bounded: 2**failures grows without limit while stalled
        # (float overflow after ~1024 failures with a long stall limit).
        base = min(_BACKOFF_MAX_S, _BACKOFF_INITIAL_S * 2 ** min(failures, 16))
        delay = base * (0.5 + 0.5 * rng())
        failures += 1
        stalled_for = clock() - last_progress
        if stalled_for + delay > stall_limit_s:
            logger.error(
                "Firmware download made no progress for %.0f s; giving up (%s)",
                stalled_for,
                result.detail,
            )
            return result
        logger.warning(
            "Firmware download interrupted (%s) at %d/%s bytes; retrying in %.0f s",
            result.detail,
            size,
            expected_size if expected_size is not None else "?",
            delay,
        )
        if wait(delay):
            logger.info("Shutdown during download backoff; resuming on next start")
            return DownloadResult(DownloadOutcome.ABORTED, "agent shutting down")


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

    # Re-stamp the slot's filesystem label. install_to_slot streams the
    # served rootfs image straight onto the block device, and that image
    # carries the *source* partition's label (root_a). Writing it to slot B
    # therefore leaves B's filesystem labelled root_a — and x86 GRUB locates
    # the slot with `search --label root_b`, so it can no longer find B and
    # the trial boot fails -> rollback. Fix it before arming the trial.
    #
    # This is mandatory only under GRUB (which searches by fs-label): a bad or
    # failed label makes the trial unbootable, so abort with a clean FAILED
    # rather than a later mystery rollback. u-boot locates the slot by GPT
    # PARTLABEL (untouched by the raw write) and ignores the fs-label, so a
    # relabel failure there must NOT fail an otherwise-good OTA — warn and
    # continue.
    if not set_slot_fs_label(target_dev, target_slot):
        if bl == "grub":
            logger.error(
                "Failed to label slot %s filesystem as root_%s — GRUB needs it to boot the slot",
                target_slot,
                target_slot,
            )
            return False
        # set_slot_fs_label already logged the failure detail at WARNING; this
        # is just the (expected, non-problematic) decision to proceed, so INFO.
        logger.info(
            "Continuing OTA despite fs-label not set on slot %s "
            "(bootloader %s does not use the fs-label to locate the slot)",
            target_slot,
            bl,
        )

    if not set_upgrade_pending(bl, target_slot):
        logger.error("Failed to set bootloader to trial-boot slot %s", target_slot)
        return False

    # H2: write a marker so detect_boot knows a trial boot was deliberately
    # armed.  Without it, _bootloader_rollback can't distinguish an ordinary
    # reboot-after-commit from a real bootloader rollback (both show
    # upgrade_available="0").  Failure is non-fatal — marker absent means
    # ota_rollback detection simply degrades to never firing (acceptable).
    try:
        state_dir = config.state_dir
        mark_pending_upgrade(state_dir, target_slot)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not write pending-upgrade marker: %s", exc)

    logger.info("Update applied to slot %s — reboot to activate", target_slot)
    return True


def set_slot_fs_label(device: str, slot: str) -> bool:
    """Stamp the ext4 filesystem label of a freshly-written slot as root_<slot>.

    Required because the OTA writes a raw rootfs image (labelled root_a) onto
    the target slot, clobbering its own label; x86 GRUB's ``search --label
    root_<slot>`` then fails to find the slot. Returns False on any failure
    (including tune2fs being absent). Logs at WARNING only — the caller decides
    the severity (fatal under GRUB, ignorable under u-boot which boots by
    PARTLABEL).
    """
    label = f"root_{slot}"
    try:
        subprocess.run(
            ["tune2fs", "-L", label, device],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        logger.warning("tune2fs not found — cannot set filesystem label %s on %s", label, device)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("tune2fs -L %s %s timed out", label, device)
        return False
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "tune2fs -L %s %s failed (rc=%s): %s", label, device, exc.returncode, exc.stderr
        )
        return False
    except OSError as exc:
        logger.warning("tune2fs -L %s %s errored: %s", label, device, exc)
        return False

    logger.info("Set filesystem label %s on %s", label, device)
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
    # Deliberately NOT O_SYNC. On the real CM4 the SD does ~5 MB/s and O_SYNC
    # (a device sync on every 1 MiB write) dragged the ~2 GB rootfs write to
    # 11-14 min while saturating the I/O bus, until systemd could no longer
    # pet the 14s BCM2835 hardware watchdog (/dev/watchdog0) and the board
    # reset mid-write — the A/B trial was never armed and it rebooted onto the
    # old slot. Buffered writes let the kernel schedule the I/O fairly (PID1
    # stays responsive); durability is guaranteed by the periodic fdatasync
    # below plus the final fsync. QEMU never hit this (fast virtual disk).
    fd = os.open(partition_device, os.O_WRONLY)
    try:
        written_since_sync = 0
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
                written_since_sync += len(chunk)
                if written_since_sync >= _SYNC_INTERVAL:
                    # Flush this batch to the device and drop the now-clean
                    # pages, so dirty-page writeback stays bounded and the
                    # page cache doesn't thrash across a multi-GB write.
                    os.fdatasync(fd)
                    try:
                        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    except (AttributeError, OSError):
                        # fadvise is best-effort (missing on some platforms /
                        # unsupported on the fd); durability is unaffected.
                        pass
                    written_since_sync = 0
        os.fsync(fd)
    finally:
        os.close(fd)
