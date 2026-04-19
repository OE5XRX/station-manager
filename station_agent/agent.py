"""Main agent loop for the Station Agent."""

import logging
import os
import re
import signal
import sys
import threading

from .config import load_config
from .health_check import run_health_checks
from .heartbeat import send_heartbeat
from .http_client import HttpClient
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
        dest_path = os.path.join(config.download_dir, f"firmware-{safe_version}.wic.bz2")

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

        if not apply_update(config, dest_path):
            report_status(
                config,
                http_client,
                result_pk,
                "failed",
                error_message="Failed to write firmware to inactive partition",
            )
            return

        # Report rebooting (in production, the device would reboot here)
        report_status(config, http_client, result_pk, "rebooting")
        logger.info("OTA update applied. In production, device would reboot now.")

        # Since we are not actually rebooting, run verification immediately.
        # In production, this would happen on the next boot.
        self._verify_and_commit(config, http_client, result_pk, version)

    def _verify_and_commit(self, config, http_client, result_pk, version):
        """Run health checks and commit or roll back the update."""
        report_status(config, http_client, result_pk, "verifying")

        # Guard against a silent bootloader rollback: if we're here from
        # a REBOOTING/VERIFYING resume, the kernel that's actually
        # running may be the old slot. Health checks alone don't
        # distinguish the two, so confirm /etc/os-release matches the
        # target tag before accepting "committed".
        from .inventory import get_current_version

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
