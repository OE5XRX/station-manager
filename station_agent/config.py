"""Configuration loader for the Station Agent."""

import logging
import os
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/stationagent/config.yml"
CONFIG_PATH_ENV = "STATION_AGENT_CONFIG"


@dataclass
class AgentConfig:
    """Station Agent configuration."""

    server_url: str = ""
    station_id: int = 0
    ed25519_key_path: str = ""
    heartbeat_interval: int = 60
    ota_check_interval: int = 5
    download_dir: str = "/tmp/station-agent"
    log_level: str = "INFO"
    terminal_enabled: bool = False
    terminal_shell: str = "/bin/sh"
    bootloader: str = "auto"
    slot_discovery_enabled: bool = True
    slot_dev_base: str = "/dev/oe5xrx"
    trace_serial: bool = False
    control_enabled: bool = False
    control_dead_man_timeout: float = 1.5
    # Whole-command slot round-trip budget. Must exceed the module's worst-case
    # firmware timeout (SA818 AT ~2 s) so a real device error surfaces as itself
    # rather than a generic timeout, and stay below the server command timeout.
    slot_command_timeout: float = 5.0
    telemetry_default_interval_ms: int = 1000
    telemetry_min_floor_ms: int = 200
    # Audio subsystem (Session B). Off by default; needs the audio-capable image (PipeWire +
    # GStreamer) on the target. The runtime shells out to gst-launch/pw-*/wpctl — no new deps.
    audio_enabled: bool = False
    audio_rx_rate: int = 8000  # FM module native (8 kHz NB); op.mic uplink is 16 kHz WB
    audio_mic_rate: int = 16000
    audio_udp_port_base: int = 47000
    audio_dead_man_timeout: float = 1.5
    audio_router_slot: int = 1000  # synthetic control-plane slot for the audio-router module
    audio_sysfs_sound: str = "/sys/class/sound"

    def validate(self):
        """Validate that required fields are present."""
        errors = []
        if not self.server_url:
            errors.append("server_url is required")
        if not self.ed25519_key_path:
            errors.append("ed25519_key_path is required")
        if not self.station_id:
            errors.append("station_id is required")
        if self.heartbeat_interval < 10:
            errors.append("heartbeat_interval must be at least 10 seconds")
        if self.ota_check_interval < 1:
            errors.append("ota_check_interval must be at least 1")
        if errors:
            raise ValueError(f"Configuration errors: {'; '.join(errors)}")


def load_config() -> AgentConfig:
    """Load configuration from YAML file.

    Config path is resolved in order:
    1. STATION_AGENT_CONFIG environment variable
    2. /etc/stationagent/config.yml
    """
    config_path = os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)
    logger.info("Loading config from %s", config_path)

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise FileNotFoundError(f"Config file not found: {config_path}")
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in config file: {exc}")

    config = AgentConfig(
        server_url=str(data.get("server_url", "")).rstrip("/"),
        station_id=int(data.get("station_id", 0)),
        ed25519_key_path=str(data.get("ed25519_key_path", "")),
        heartbeat_interval=int(data.get("heartbeat_interval", 60)),
        ota_check_interval=int(data.get("ota_check_interval", 5)),
        download_dir=str(data.get("download_dir", "/tmp/station-agent")),
        log_level=str(data.get("log_level", "INFO")).upper(),
        terminal_enabled=bool(data.get("terminal_enabled", False)),
        terminal_shell=str(data.get("terminal_shell", "/bin/sh")),
        bootloader=str(data.get("bootloader", "auto")),
        slot_discovery_enabled=bool(data.get("slot_discovery_enabled", True)),
        slot_dev_base=str(data.get("slot_dev_base", "/dev/oe5xrx")),
        trace_serial=bool(data.get("trace_serial", False)),
        control_enabled=bool(data.get("control_enabled", False)),
        control_dead_man_timeout=float(data.get("control_dead_man_timeout", 1.5)),
        slot_command_timeout=float(data.get("slot_command_timeout", 5.0)),
        telemetry_default_interval_ms=int(data.get("telemetry_default_interval_ms", 1000)),
        telemetry_min_floor_ms=int(data.get("telemetry_min_floor_ms", 200)),
        audio_enabled=bool(data.get("audio_enabled", False)),
        audio_rx_rate=int(data.get("audio_rx_rate", 8000)),
        audio_mic_rate=int(data.get("audio_mic_rate", 16000)),
        audio_udp_port_base=int(data.get("audio_udp_port_base", 47000)),
        audio_dead_man_timeout=float(data.get("audio_dead_man_timeout", 1.5)),
        audio_router_slot=int(data.get("audio_router_slot", 1000)),
        audio_sysfs_sound=str(data.get("audio_sysfs_sound", "/sys/class/sound")),
    )
    config.validate()
    return config
