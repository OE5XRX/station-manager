"""Configuration loader for the Station Agent."""

import logging
import os
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/station-agent/config.yml"
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
    terminal_shell: str = "/bin/bash"
    bootloader: str = "auto"

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
    2. /etc/station-agent/config.yml
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
        terminal_shell=str(data.get("terminal_shell", "/bin/bash")),
        bootloader=str(data.get("bootloader", "auto")),
    )
    config.validate()
    return config
