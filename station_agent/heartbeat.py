"""Heartbeat module for the Station Agent.

Collects system information and sends it to the Station Manager server.
"""

import logging
import socket
import time

from . import __version__
from .http_client import HttpClient
from .inventory import collect_inventory

logger = logging.getLogger(__name__)

_OS_RELEASE_PATH = "/etc/os-release"


def get_hostname() -> str:
    """Return the system hostname."""
    return socket.gethostname()


def get_os_version() -> str:
    """Read OS version from /etc/os-release."""
    try:
        with open(_OS_RELEASE_PATH) as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        logger.debug("Could not read /etc/os-release")
    return "unknown"


def get_image_variant() -> str:
    """Read the baked image channel (VARIANT_ID) from /etc/os-release."""
    try:
        with open(_OS_RELEASE_PATH) as f:
            for line in f:
                if line.startswith("VARIANT_ID="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        logger.debug("Could not read /etc/os-release for VARIANT_ID")
    return ""


def get_uptime() -> float:
    """Read system uptime in seconds from /proc/uptime."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except OSError:
        logger.debug("Could not read /proc/uptime")
    return 0.0


def get_ip_address() -> str:
    """Get the primary IP address of this machine."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        logger.debug("Could not determine IP address")
    return "0.0.0.0"


def get_module_versions() -> dict:
    """Collect firmware versions of connected modules.

    This is a placeholder. Actual implementation will depend on
    the hardware modules connected to the station (e.g., TRX, rotor,
    power supply controllers).
    """
    return {}


def _collect_telemetry_safe(config):
    from station_agent import telemetry

    try:
        return telemetry.collect_telemetry(config)
    except Exception:  # noqa: BLE001
        logger.debug("telemetry collection failed; sending empty telemetry")
        return {}


def collect_system_info(config=None) -> dict:
    """Collect all system information for the heartbeat payload.

    Args:
        config: Optional AgentConfig. Forwarded to ``collect_inventory`` so
            that slot discovery is enabled/disabled per configuration.
    """
    return {
        "hostname": get_hostname(),
        "os_version": get_os_version(),
        "uptime": get_uptime(),
        "ip_address": get_ip_address(),
        "agent_version": __version__,
        "image_variant": get_image_variant(),
        "module_versions": get_module_versions(),
        "inventory": collect_inventory(config=config),
        "telemetry": _collect_telemetry_safe(config),
        "timestamp": time.time(),
    }


def send_heartbeat(http_client: HttpClient, config=None, tx_settings=None) -> bool:
    """Send a heartbeat to the Station Manager server.

    Args:
        http_client: Authenticated HTTP client.
        config: Optional AgentConfig. Forwarded to ``collect_system_info`` so
            that slot discovery is enabled/disabled per configuration.
        tx_settings: Optional ``TxAudioSettings`` fed from the response body.

    Returns:
        True if the heartbeat was sent successfully, False otherwise.
    """
    payload = collect_system_info(config=config)

    response = http_client.request("POST", "/api/v1/heartbeat/", json_data=payload)
    if response is None:
        return False

    if response.status_code == 200:
        logger.info("Heartbeat sent successfully")
        if tx_settings is not None:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - e.g. RecursionError on hostile JSON
                logger.warning("Heartbeat response body unparseable; TX ceiling not raised")
                body = None
            tx_settings.update_from_heartbeat(body)
        return True
    else:
        logger.warning(
            "Heartbeat rejected by server: %s %s",
            response.status_code,
            response.text[:200],
        )
        return False
