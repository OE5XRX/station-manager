"""Shared HTTP client with Ed25519 authentication for the Station Agent.

Provides a single entry point for all server communication, signing every
outbound request with the device's Ed25519 private key.
"""

import json
import logging

import requests

from . import __version__
from .config import AgentConfig
from .signing import load_private_key, sign_request

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class HttpClient:
    """HTTP client that handles authentication and base URL construction."""

    def __init__(self, config: AgentConfig):
        self._config = config
        self._private_key = load_private_key(config.ed25519_key_path)
        if self._private_key is None:
            raise RuntimeError(
                f"Ed25519 private key could not be loaded from {config.ed25519_key_path}"
            )

    def request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        stream: bool = False,
    ) -> requests.Response | None:
        """Send an authenticated HTTP request to the server.

        Args:
            method: HTTP method (GET, POST, PUT, etc.).
            path: API path (e.g., "/api/v1/heartbeat/").
            json_data: Optional JSON-serializable body.
            timeout: Request timeout in seconds.
            stream: If True, stream the response body.

        Returns:
            The requests.Response object, or None on connection failure.
        """
        url = f"{self._config.server_url}{path}"

        # Serialize body so signing hash matches the exact bytes the server sees.
        # When requests sends json=, it uses json.dumps with default separators
        # and utf-8 encoding. We replicate that here for signing.
        if json_data is not None:
            body_bytes = json.dumps(json_data).encode("utf-8")
        else:
            body_bytes = b""

        headers = {
            "User-Agent": f"StationAgent/{__version__}",
            "Content-Type": "application/json",
        }

        auth_headers = sign_request(self._private_key, self._config.station_id, body_bytes)
        headers.update(auth_headers)

        try:
            # Send body_bytes directly instead of json= to ensure the exact
            # bytes match what was signed.
            response = requests.request(
                method,
                url,
                data=body_bytes if json_data is not None else None,
                headers=headers,
                timeout=timeout,
                stream=stream,
            )
            return response
        except requests.ConnectionError:
            logger.error("Cannot connect to server at %s", url)
            return None
        except requests.Timeout:
            logger.error("Request to %s timed out after %ds", url, timeout)
            return None
        except requests.RequestException as exc:
            logger.error("Request to %s failed: %s", url, exc)
            return None
