"""Hardware inventory collection for the Station Agent.

Gathers CPU, RAM, disk, network, and OS information from the local
system. All reads handle missing files gracefully for embedded Linux
environments where some paths may not exist.
"""

import logging
import os
import re
import socket

logger = logging.getLogger(__name__)

_OS_RELEASE_PATH = "/etc/os-release"


def get_current_version() -> str:
    """Return the OE5XRX firmware release tag from /etc/os-release, or ""."""
    try:
        with open(_OS_RELEASE_PATH) as f:
            for line in f:
                if line.startswith("OE5XRX_RELEASE="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _read_file(path: str) -> str:
    """Read a file and return its contents, or empty string on failure."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        logger.debug("Could not read %s", path)
        return ""


def _get_cpu_info() -> dict:
    """Collect CPU model, core count, and temperature."""
    info = {"model": "unknown", "cores": 0, "temperature_c": None}

    cpuinfo = _read_file("/proc/cpuinfo")
    if cpuinfo:
        # ARM: "model name" or "Hardware" line
        for line in cpuinfo.splitlines():
            if line.startswith("model name") or line.startswith("Model"):
                info["model"] = line.split(":", 1)[1].strip()
                break
        # Count processor entries
        info["cores"] = len(re.findall(r"^processor\s*:", cpuinfo, re.MULTILINE))

    # Try thermal zone 0 (common on RPi)
    temp_str = _read_file("/sys/class/thermal/thermal_zone0/temp").strip()
    if temp_str:
        try:
            info["temperature_c"] = round(int(temp_str) / 1000.0, 1)
        except ValueError:
            pass

    return info


def _get_ram_info() -> dict:
    """Collect RAM total, free, and usage percentage."""
    info = {"total_mb": 0, "free_mb": 0, "usage_percent": 0.0}

    meminfo = _read_file("/proc/meminfo")
    if not meminfo:
        return info

    values = {}
    for line in meminfo.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            try:
                values[key] = int(parts[1])  # in kB
            except ValueError:
                continue

    total_kb = values.get("MemTotal", 0)
    available_kb = values.get("MemAvailable", values.get("MemFree", 0))

    info["total_mb"] = round(total_kb / 1024)
    info["free_mb"] = round(available_kb / 1024)
    if total_kb > 0:
        used = total_kb - available_kb
        info["usage_percent"] = round(used / total_kb * 100, 1)

    return info


def _get_disk_info() -> list[dict]:
    """Collect disk usage for mounted filesystems."""
    mounts = ["/", "/boot"]
    disks = []

    for mount in mounts:
        try:
            st = os.statvfs(mount)
        except OSError:
            continue

        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        total_mb = round(total / (1024 * 1024))
        free_mb = round(free / (1024 * 1024))
        usage = round((1 - free / total) * 100, 1) if total > 0 else 0.0

        disks.append(
            {
                "mount": mount,
                "total_mb": total_mb,
                "free_mb": free_mb,
                "usage_percent": usage,
            }
        )

    return disks


def _get_network_info() -> list[dict]:
    """Collect network interface information from /sys/class/net/."""
    interfaces = []
    net_dir = "/sys/class/net"

    try:
        iface_names = os.listdir(net_dir)
    except OSError:
        logger.debug("Could not list %s", net_dir)
        return interfaces

    for iface in sorted(iface_names):
        if iface == "lo":
            continue

        entry = {"interface": iface, "ip_address": "", "mac_address": ""}

        # MAC address
        mac = _read_file(f"{net_dir}/{iface}/address").strip()
        if mac:
            entry["mac_address"] = mac

        # IP address via socket (best effort)
        try:
            import fcntl
            import struct

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            result = fcntl.ioctl(
                sock.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack("256s", iface.encode("utf-8")[:15]),
            )
            entry["ip_address"] = socket.inet_ntoa(result[20:24])
            sock.close()
        except Exception:
            pass

        interfaces.append(entry)

    return interfaces


def _get_os_info() -> dict:
    """Collect OS version, kernel, and uptime."""
    info = {"version": "unknown", "kernel": "", "uptime_seconds": 0.0}

    # OS version from /etc/os-release
    os_release = _read_file("/etc/os-release")
    for line in os_release.splitlines():
        if line.startswith("PRETTY_NAME="):
            info["version"] = line.split("=", 1)[1].strip().strip('"')
            break

    # Kernel
    try:
        uname = os.uname()
        info["kernel"] = f"{uname.sysname} {uname.release}"
    except Exception:
        pass

    # Uptime
    uptime_str = _read_file("/proc/uptime").strip()
    if uptime_str:
        try:
            info["uptime_seconds"] = float(uptime_str.split()[0])
        except (ValueError, IndexError):
            pass

    return info


def collect_inventory() -> dict:
    """Collect complete hardware inventory.

    Returns:
        Dict with keys: cpu, ram, disk, network, os.
        All values are safe to serialize to JSON.
    """
    try:
        return {
            "cpu": _get_cpu_info(),
            "ram": _get_ram_info(),
            "disk": _get_disk_info(),
            "network": _get_network_info(),
            "os": _get_os_info(),
        }
    except Exception as exc:
        logger.error("Inventory collection failed: %s", exc)
        return {}
