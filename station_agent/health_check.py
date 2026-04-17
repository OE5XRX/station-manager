"""Pre-commit health checks for the Station Agent.

Run before committing a firmware update to verify the system is
in a healthy state. All checks are defensive and never crash the agent.
"""

import logging
import os
import subprocess

import requests

logger = logging.getLogger(__name__)


def _check_network(server_url: str) -> tuple[bool, str]:
    """Check that the server is reachable.

    Args:
        server_url: Base URL of the Station Manager server.

    Returns:
        Tuple of (passed, message).
    """
    url = f"{server_url}/api/v1/health/"
    try:
        response = requests.get(url, timeout=10)
        # Any response (even 405) means the server is reachable
        return True, f"Network OK: server reachable (HTTP {response.status_code})"
    except requests.ConnectionError:
        return False, f"Network FAIL: cannot connect to {server_url}"
    except requests.Timeout:
        return False, f"Network FAIL: connection to {server_url} timed out"
    except requests.RequestException as exc:
        return False, f"Network FAIL: {exc}"


def _check_disk() -> tuple[bool, str]:
    """Check that the root partition has more than 5% free space."""
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total == 0:
            return False, "Disk FAIL: root partition reports 0 total bytes"
        free_pct = (free / total) * 100
        if free_pct < 5.0:
            return False, f"Disk FAIL: root partition has only {free_pct:.1f}% free"
        return True, f"Disk OK: root partition has {free_pct:.1f}% free"
    except OSError as exc:
        return False, f"Disk FAIL: cannot stat root partition: {exc}"


def _check_systemd_service() -> tuple[bool, str]:
    """Check that station-agent.service is active via systemctl."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "station-agent.service"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        status = result.stdout.strip()
        if status == "active":
            return True, "Systemd OK: station-agent.service is active"
        return False, f"Systemd WARN: station-agent.service is {status}"
    except FileNotFoundError:
        # systemctl not available (e.g., container or non-systemd init)
        return True, "Systemd SKIP: systemctl not found, skipping check"
    except subprocess.TimeoutExpired:
        return False, "Systemd FAIL: systemctl timed out"
    except Exception as exc:
        return False, f"Systemd FAIL: {exc}"


def run_health_checks(server_url: str = "") -> tuple[bool, list[str]]:
    """Run all pre-commit health checks.

    Args:
        server_url: Base URL of the Station Manager server for network check.

    Returns:
        Tuple of (all_passed, list_of_messages).
    """
    messages = []
    all_passed = True

    checks = [
        ("disk", lambda: _check_disk()),
        ("systemd", lambda: _check_systemd_service()),
    ]

    # Only run network check if a server URL is provided
    if server_url:
        checks.insert(0, ("network", lambda: _check_network(server_url)))

    for name, check_fn in checks:
        try:
            passed, msg = check_fn()
            messages.append(msg)
            if not passed:
                all_passed = False
                logger.warning("Health check '%s' failed: %s", name, msg)
            else:
                logger.info("Health check '%s' passed: %s", name, msg)
        except Exception as exc:
            all_passed = False
            msg = f"{name} ERROR: unexpected exception: {exc}"
            messages.append(msg)
            logger.error("Health check '%s' raised exception: %s", name, exc)

    return all_passed, messages
