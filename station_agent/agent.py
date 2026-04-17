"""Main agent loop for the Station Agent."""

import logging
import os
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
    download_firmware,
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

        Flow: check -> report downloading -> download -> verify checksum ->
              report installing -> apply -> report rebooting.

        On next startup (or after simulated reboot), health checks run
        and the boot is committed.
        """
        deployment = check_for_update(config, http_client)
        if deployment is None:
            return

        result_pk = deployment.get("result_id")
        download_url = deployment.get("download_url", "")
        version = deployment.get("firmware_version", "")
        is_delta = deployment.get("is_delta", False)

        # Use delta checksum when downloading a delta, full checksum otherwise
        if is_delta and deployment.get("delta_checksum_sha256"):
            expected_checksum = deployment["delta_checksum_sha256"]
        else:
            expected_checksum = deployment.get("checksum_sha256", "")

        if not result_pk or not download_url:
            logger.error("Deployment response missing required fields")
            return

        # Report downloading
        report_status(config, http_client, result_pk, "downloading")

        # Download firmware
        suffix = ".xdelta3" if is_delta else ".bin"
        dest_path = os.path.join(config.download_dir, f"firmware-{version}{suffix}")
        if not download_firmware(config, http_client, download_url, expected_checksum, dest_path):
            report_status(
                config,
                http_client,
                result_pk,
                "failed",
                error_message="Firmware download or checksum verification failed",
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
