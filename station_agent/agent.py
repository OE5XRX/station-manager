"""Main agent loop for the Station Agent."""

import logging
import os
import re
import signal
import subprocess
import sys
import threading

from .bootloader import get_bootloader, get_env
from .config import load_config
from .health_check import run_health_checks
from .heartbeat import send_heartbeat
from .http_client import HttpClient
from .inventory import get_current_version
from .ota import (
    apply_update,
    check_for_update,
    commit_boot,
    download_firmware_resumable,
    report_status,
)
from .terminal import TerminalClient

logger = logging.getLogger("station_agent")


class StationAgent:
    """Main agent that runs the heartbeat loop with OTA updates."""

    def __init__(self):
        self._shutdown = threading.Event()

    def _handle_signal(self, signum, frame):
        """Handle termination signals for graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down...", sig_name)
        self._shutdown.set()

    def _handle_ota(self, config, http_client):
        """Check for and process an OTA update.

        Flow is state-driven off `deployment_result_status` from the
        server so a crash-recover restart picks up where it left off
        without overwriting the rollback slot:

          PENDING / DOWNLOADING  -> download -> install -> reboot -> verify
          INSTALLING             -> install -> reboot -> verify
          REBOOTING / VERIFYING  -> verify+commit only (image is already
                                    on disk; we're booted into it, so
                                    writing again to the *now-inactive*
                                    slot would corrupt the known-good
                                    rollback target)
        """
        deployment = check_for_update(config, http_client)
        if deployment is None:
            return

        result_pk = deployment.get("deployment_result_id")
        download_url = deployment.get("download_url", "")
        version = deployment.get("target_tag", "")
        expected_checksum = deployment.get("checksum_sha256", "")
        expected_size = deployment.get("size_bytes") or None
        current_status = deployment.get("deployment_result_status", "pending")

        if not result_pk or not download_url:
            logger.error("Deployment response missing required fields")
            return

        # Post-reboot recovery: the image is already on the slot we're
        # running from, and the slot we'd call "inactive" is the old
        # rollback target. Running the install path here would overwrite
        # it. Go straight to health-check + commit.
        if current_status in ("rebooting", "verifying"):
            logger.info(
                "Resuming deployment %s from %s — skipping download+install",
                result_pk,
                current_status,
            )
            self._verify_and_commit(config, http_client, result_pk, version)
            return

        # Sanitize the tag before it becomes a filename: a compromised or
        # sloppy server could return a tag like "../" and turn dest_path
        # into a traversal outside download_dir.
        safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", version) or "image"
        dest_path = os.path.join(config.download_dir, f"firmware-{safe_version}.rootfs.bz2")

        # Legacy-partial sweep: old agents used `.wic.bz2` as the
        # suffix. After the server-side switch to rootfs artifacts
        # (PR #28) any such partial left in download_dir is
        # unreachable (the new filename can't match it) and would
        # accumulate forever. The glob runs on every _handle_ota
        # call; steady state has nothing to remove and the readdir
        # is free.
        import glob as _glob

        for stale in _glob.glob(os.path.join(config.download_dir, "firmware-*.wic.bz2")):
            try:
                os.remove(stale)
                logger.info("Removed legacy partial: %s", stale)
            except OSError as exc:
                logger.warning("Could not remove legacy partial %s: %s", stale, exc)

        if current_status != "installing":
            # Report downloading (idempotent — transitioning PENDING or
            # stale DOWNLOADING both map to "downloading now").
            report_status(config, http_client, result_pk, "downloading")

            # Download firmware (resumable; download_url is opaque — pass verbatim).
            if not download_firmware_resumable(
                http_client=http_client,
                download_url=download_url,
                expected_checksum=expected_checksum,
                dest_path=dest_path,
                expected_size=expected_size,
            ):
                report_status(
                    config,
                    http_client,
                    result_pk,
                    "failed",
                    error_message="Firmware download or checksum verification failed",
                )
                return
        else:
            # Resuming from INSTALLING means the download completed once
            # before. If the file is still there, trust it (checksum was
            # already verified). If it's gone (e.g. /tmp was cleared on
            # reboot), re-download before re-installing.
            if not os.path.exists(dest_path):
                logger.info("INSTALLING resume but partial is gone — re-downloading")
                report_status(config, http_client, result_pk, "downloading")
                if not download_firmware_resumable(
                    http_client=http_client,
                    download_url=download_url,
                    expected_checksum=expected_checksum,
                    dest_path=dest_path,
                    expected_size=expected_size,
                ):
                    report_status(
                        config,
                        http_client,
                        result_pk,
                        "failed",
                        error_message="Firmware re-download failed during INSTALLING resume",
                    )
                    return

        # Report installing
        report_status(config, http_client, result_pk, "installing")

        try:
            install_ok = apply_update(config, dest_path)
        except RuntimeError as exc:
            # Slot-detection fail-closed path (both /proc/cmdline and
            # root-mount probes unresolved). Forward the specific reason
            # into the server-visible error_message so the operator can
            # diagnose without needing local logs.
            logger.error("Install aborted: %s", exc)
            report_status(
                config,
                http_client,
                result_pk,
                "failed",
                error_message=f"Install aborted: {exc}",
            )
            return
        if not install_ok:
            report_status(
                config,
                http_client,
                result_pk,
                "failed",
                error_message="Failed to write firmware to inactive partition",
            )
            return

        # Report rebooting first so the server knows the station is
        # going down intentionally; a failed report here must not
        # block the reboot itself.
        report_status(config, http_client, result_pk, "rebooting")

        # Real reboot. systemctl queues the reboot with systemd and
        # returns 0 immediately — we are not killed until systemd
        # tears down services (typically a few seconds later). We
        # must not return here: the heartbeat loop's next check would
        # hit the post-reboot-recovery path BEFORE the reboot
        # actually happened and verify against the still-running-old
        # rootfs. Block on the shutdown event until systemd signals
        # us. _verify_and_commit then fires on the NEXT boot via the
        # post-reboot-recovery path at the top of _handle_ota
        # (deployment_result_status in {"rebooting", "verifying"}).
        try:
            subprocess.run(
                ["systemctl", "reboot"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            # systemctl/dbus hung. The station would otherwise sit in
            # "rebooting" state indefinitely.
            stderr = getattr(exc, "stderr", None) or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            logger.error(
                "Reboot command timed out after %ss%s",
                exc.timeout,
                f" — {stderr.strip()}" if stderr else "",
            )
            report_status(
                config,
                http_client,
                result_pk,
                "failed",
                error_message=(
                    f"Reboot call timed out after {exc.timeout}s"
                    + (f" — {stderr.strip()}" if stderr else "")
                ),
            )
            return
        except (OSError, subprocess.CalledProcessError) as exc:
            # No systemctl, permission denied, unit refused, etc. —
            # slot_b is written and armed but the switch didn't
            # happen. capture_output=True on the successful path
            # costs nothing (systemctl reboot is silent on success)
            # but on failure lets us forward systemd's actual
            # refusal reason (inhibitor name, polkit denial, unit
            # stuck) through to the server so the operator can
            # diagnose without ssh'ing to a brick.
            stderr = getattr(exc, "stderr", None) or ""
            logger.error("Reboot failed: %s%s", exc, f" — {stderr.strip()}" if stderr else "")
            report_status(
                config,
                http_client,
                result_pk,
                "failed",
                error_message=(
                    f"Reboot call failed: {exc}" + (f" — {stderr.strip()}" if stderr else "")
                ),
            )
            return

        logger.info("Reboot queued — waiting for systemd shutdown signal")
        # Block for up to 5 minutes. SIGTERM from systemd sets the
        # shutdown event → wait returns True → normal shutdown path.
        # If 5 minutes pass with the process still alive, something
        # went wrong (inhibitor, systemd stuck, ...) — report FAILED
        # so the operator sees the station stuck on "rebooting"
        # isn't actually going to reboot.
        if self._shutdown.wait(timeout=300):
            return

        logger.error("Reboot queued 5 minutes ago but shutdown signal never came")
        report_status(
            config,
            http_client,
            result_pk,
            "failed",
            error_message=(
                "Reboot queued via systemctl but shutdown signal never "
                "arrived within 5 minutes — reboot was likely inhibited."
            ),
        )

    def _verify_and_commit(self, config, http_client, result_pk, version):
        """Run health checks and commit or roll back the update."""
        report_status(config, http_client, result_pk, "verifying")

        # Guard against a silent bootloader rollback: if we're here from
        # a REBOOTING/VERIFYING resume, the kernel that's actually
        # running may be the old slot. Health checks alone don't
        # distinguish the two, so confirm /etc/os-release matches the
        # target tag before accepting "committed".
        running_version = get_current_version()
        if not running_version:
            # /etc/os-release doesn't expose OE5XRX_RELEASE — we cannot
            # prove what image we're actually running. Fail closed: if
            # the bootloader rolled back we'd otherwise commit the new
            # deployment against the old kernel.
            report_status(
                config,
                http_client,
                result_pk,
                "rolled_back",
                error_message=(
                    "Cannot verify running version (OE5XRX_RELEASE not found "
                    "in /etc/os-release); refusing to commit."
                ),
            )
            logger.warning("Refusing to commit deployment %s: running version unknown", result_pk)
            return
        if running_version != version:
            report_status(
                config,
                http_client,
                result_pk,
                "rolled_back",
                error_message=(
                    f"Bootloader rolled back: running {running_version!r}, expected {version!r}"
                ),
            )
            logger.warning(
                "Refusing to commit: running %s but deployment target is %s",
                running_version,
                version,
            )
            return

        # Trial-flag guard: both oe5xrx-grub.cfg and boot.cmd clear
        # upgrade_available to 0 when they roll back after
        # bootcount > bootlimit. Reading it post-reboot catches the
        # same-version-redeploy blind spot the version check above
        # cannot see (both slots return the same /etc/os-release
        # tag, so running_version == target is not a proof the
        # bootloader didn't swap back). None also means rolled_back
        # — env read failed and we refuse to commit blind.
        bl = get_bootloader(config)
        try:
            upgrade_available = get_env(bl, "upgrade_available")
            bootcount = get_env(bl, "bootcount")
        except PermissionError as exc:
            # get_env already swallows missing-tool (FileNotFoundError)
            # and hung-tool (TimeoutExpired) by returning None. What
            # can still propagate is a PermissionError reading the env
            # blob itself (e.g. /boot/EFI/BOOT/grubenv owned by root
            # on a mis-provisioned image). Fail closed so we don't
            # commit blind on an unreadable trial state.
            logger.warning(
                "Failed to read bootloader env for deployment %s: %s",
                result_pk,
                exc,
            )
            upgrade_available = None
            bootcount = None

        # Three cases, distinguished by the (upgrade_available, bootcount)
        # pair that commit_boot_local and the bootloader's rollback path
        # write:
        #   "1", any      -> trial active, proceed to commit.
        #   "0", "0"      -> local commit already ran on a prior
        #                    _verify_and_commit pass (commit_boot_local
        #                    sets both to "0") AND no reboot happened
        #                    since (bootloader increments bootcount on
        #                    every boot). Server POST must have failed
        #                    transiently — retry commit_boot (idempotent
        #                    at the local level).
        #   "0", anything else -> bootloader rolled us back
        #                         (boot.cmd / oe5xrx-grub.cfg set
        #                         bootcount=1 on their rollback branch).
        #   None, any     -> env unreadable; fail closed.
        #
        # Known limitation: a reboot between local commit and server-commit
        # retry moves bootcount to >=1 and this guard would then misclassify
        # as rolled_back. Persistent commit marker is tracked as a follow-up.
        if upgrade_available == "1":
            pass  # trial active, normal path
        elif upgrade_available == "0" and bootcount == "0":
            logger.info(
                "Detected prior local commit for deployment %s "
                "(upgrade_available=0, bootcount=0); retrying server commit",
                result_pk,
            )
        else:
            report_status(
                config,
                http_client,
                result_pk,
                "rolled_back",
                error_message=(
                    f"Bootloader upgrade_available={upgrade_available!r}, "
                    f"bootcount={bootcount!r} — trial boot was rolled back "
                    "or env read failed; refusing to commit."
                ),
            )
            logger.warning(
                "Refusing to commit deployment %s: upgrade_available=%r, bootcount=%r",
                result_pk,
                upgrade_available,
                bootcount,
            )
            return

        passed, messages = run_health_checks(server_url=config.server_url)
        for msg in messages:
            logger.info("Health: %s", msg)

        if passed:
            if commit_boot(config, http_client, version):
                logger.info("Update to version %s committed successfully", version)
            else:
                logger.error("Failed to commit boot for version %s", version)
        else:
            error = "; ".join(m for m in messages if "FAIL" in m)
            report_status(
                config,
                http_client,
                result_pk,
                "rolled_back",
                error_message=f"Health checks failed: {error}",
            )
            logger.warning("Update rolled back due to failed health checks")

    def run(self):
        """Load config, set up logging, and run the heartbeat loop."""
        # Load configuration first (basic logging until config is loaded)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stderr,
        )

        try:
            config = load_config()
        except (FileNotFoundError, ValueError) as exc:
            logger.critical("Configuration error: %s", exc)
            sys.exit(1)

        # Reconfigure logging with the level from config
        logging.getLogger().setLevel(getattr(logging, config.log_level, logging.INFO))

        logger.info("Station Agent starting")
        logger.info("Server: %s", config.server_url)
        logger.info("Heartbeat interval: %ds", config.heartbeat_interval)
        logger.info("OTA check every %d heartbeats", config.ota_check_interval)

        # Set up authenticated HTTP client
        http_client = HttpClient(config)

        # Register signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Start terminal client in a background thread if enabled
        terminal_client = None
        terminal_thread = None
        if config.terminal_enabled:
            logger.info("Remote terminal enabled (shell: %s)", config.terminal_shell)
            terminal_client = TerminalClient(config)
            terminal_thread = threading.Thread(
                target=terminal_client.run,
                name="terminal-client",
                daemon=True,
            )
            terminal_thread.start()
        else:
            logger.info("Remote terminal disabled")

        # Main heartbeat loop
        heartbeat_count = 0
        while not self._shutdown.is_set():
            send_heartbeat(http_client)
            heartbeat_count += 1

            # Check for OTA updates at the configured interval
            if heartbeat_count % config.ota_check_interval == 0:
                try:
                    self._handle_ota(config, http_client)
                except Exception as exc:
                    logger.error("OTA check failed unexpectedly: %s", exc)

            self._shutdown.wait(timeout=config.heartbeat_interval)

        # Stop terminal client on shutdown
        if terminal_client is not None:
            terminal_client.stop()
        if terminal_thread is not None:
            terminal_thread.join(timeout=5)

        logger.info("Station Agent stopped")
