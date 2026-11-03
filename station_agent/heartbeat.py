"""Heartbeat module for the Station Agent.

Collects system information and sends it to the Station Manager server.
"""

import logging
import socket
import threading
import time
import weakref

from . import __version__
from .http_client import HttpClient
from .inventory import collect_inventory
from .metrics_contract import descriptors_hash
from .module_ota import detect_backends

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


class DescriptorState:
    """When to send the full ``metric_descriptors`` list (spec §2.1).

    Full list on the first heartbeat after start (``last_sent_hash is None``), on a
    hash change, and after the server answered ``metric_descriptors_required: true``.
    The hash only counts as delivered after a 200. Shared by the main-loop and the
    keepalive heartbeat thread, hence the lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.last_sent_hash: str | None = None
        self.force_send = False

    def needs_list(self, current_hash: str) -> bool:
        with self._lock:
            return self.force_send or self.last_sent_hash != current_hash

    def on_response(self, sent_hash: str | None, list_sent: bool, required: bool) -> None:
        """Record a 200 response for a heartbeat that carried ``sent_hash``."""
        with self._lock:
            if required:
                self.force_send = True
            elif list_sent and sent_hash is not None:
                self.last_sent_hash = sent_hash
                self.force_send = False


# One DescriptorState per collector (= per agent run); weak so a dropped collector
# (tests, restart) takes its state with it.
_descriptor_states: "weakref.WeakKeyDictionary[object, DescriptorState]" = (
    weakref.WeakKeyDictionary()
)
_descriptor_states_lock = threading.Lock()


def descriptor_state(metrics) -> DescriptorState:
    """Return the DescriptorState bound to ``metrics`` (created on first use)."""
    with _descriptor_states_lock:
        state = _descriptor_states.get(metrics)
        if state is None:
            state = _descriptor_states[metrics] = DescriptorState()
        return state


def _collect_metrics_safe(metrics) -> dict:
    """Heartbeat fields for the metrics block; ``{}`` if the collector fails."""
    try:
        values, descriptors = metrics.snapshot()
        current_hash = descriptors_hash(descriptors)
        fields = {"metrics": values, "metric_descriptors_hash": current_hash}
        if descriptor_state(metrics).needs_list(current_hash):
            fields["metric_descriptors"] = descriptors
        return fields
    except Exception:  # noqa: BLE001 - metrics must never cost a heartbeat
        logger.exception("metrics collection failed; heartbeat sent without metrics")
        return {}


def collect_system_info(config=None, backends=None, metrics=None) -> dict:
    """Collect all system information for the heartbeat payload.

    Args:
        config: Optional AgentConfig. Forwarded to ``collect_inventory`` so
            that slot discovery is enabled/disabled per configuration.
        backends: Module-OTA backends to advertise. The agent passes
            ``ModuleOta.advertised_backends()`` so the capability is exactly what the
            running ModuleOta can execute; None falls back to plain detection.
        metrics: Optional ``MetricsCollector``; adds ``metrics``,
            ``metric_descriptors_hash`` and (when due) ``metric_descriptors``.
    """
    if backends is None:
        backends = detect_backends(config) if config is not None else []
    info = {
        "hostname": get_hostname(),
        "os_version": get_os_version(),
        "uptime": get_uptime(),
        "ip_address": get_ip_address(),
        "agent_version": __version__,
        "image_variant": get_image_variant(),
        "module_versions": get_module_versions(),
        "inventory": collect_inventory(config=config),
        "telemetry": _collect_telemetry_safe(config),
        "capabilities": {"module_ota": {"backends": backends}} if backends else {},
        "timestamp": time.time(),
    }
    if metrics is not None:
        info.update(_collect_metrics_safe(metrics))
    return info


def send_heartbeat(
    http_client: HttpClient,
    config=None,
    tx_settings=None,
    module_ota_backends=None,
    metrics=None,
) -> bool:
    """Send a heartbeat to the Station Manager server.

    Args:
        http_client: Authenticated HTTP client.
        config: Optional AgentConfig. Forwarded to ``collect_system_info`` so
            that slot discovery is enabled/disabled per configuration.
        tx_settings: Optional ``TxAudioSettings`` fed from the response body.
        module_ota_backends: Backends to advertise (see ``collect_system_info``).
        metrics: Optional ``MetricsCollector``. Adds the metrics block, records the
            POST duration as ``net.server_rtt_ms`` (reported next heartbeat) and
            tracks descriptor delivery. None => legacy payload without metrics.

    Returns:
        True if the heartbeat was sent successfully, False otherwise.
    """
    payload = collect_system_info(config=config, backends=module_ota_backends, metrics=metrics)

    started = time.monotonic()
    response = http_client.request("POST", "/api/v1/heartbeat/", json_data=payload)
    if response is None:
        return False
    if metrics is not None:
        try:
            metrics.record_server_rtt((time.monotonic() - started) * 1000.0)
        except Exception:  # noqa: BLE001 - RTT is best effort
            logger.debug("record_server_rtt failed", exc_info=True)

    if response.status_code == 200:
        logger.info("Heartbeat sent successfully")
        body = None
        if tx_settings is not None or metrics is not None:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - e.g. RecursionError on hostile JSON
                logger.warning("Heartbeat response body unparseable; TX ceiling not raised")
                body = None
        if tx_settings is not None:
            tx_settings.update_from_heartbeat(body)
        if metrics is not None and "metric_descriptors_hash" in payload:
            required = isinstance(body, dict) and body.get("metric_descriptors_required") is True
            descriptor_state(metrics).on_response(
                payload["metric_descriptors_hash"],
                list_sent="metric_descriptors" in payload,
                required=required,
            )
        return True
    else:
        logger.warning(
            "Heartbeat rejected by server: %s %s",
            response.status_code,
            response.text[:200],
        )
        return False
