"""Client IP for audit rows — the single trusted source.

Production runs Daphne behind nginx (``deploy/nginx.conf``), which sets
``X-Real-IP $remote_addr`` on every proxied location (``/`` and ``/ws/``) and
*appends* to ``X-Forwarded-For`` (``$proxy_add_x_forwarded_for``). The first
X-Forwarded-For hop is therefore whatever the client chose to send and must
never be trusted; ``X-Real-IP`` is overwritten by nginx on every request.
Without a proxy (dev) the header is absent and ``REMOTE_ADDR`` / the ASGI
scope client is used.

Every value is validated: a malformed header must never write garbage into a
``GenericIPAddressField`` (a DB error there would suppress the audit row).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping


def parse_ip(candidate) -> str | None:
    """Return the candidate as a normalised IP string, or None if invalid.

    Besides plain IPv4/IPv6 this accepts ``IPv4:port``, ``[IPv6]:port`` and
    ``IPv6%zone``; the returned string has port/zone stripped, matching what
    ``GenericIPAddressField`` stores. Garbage returns ``None``.
    """
    if not candidate or not isinstance(candidate, str):
        return None
    stripped = candidate.strip()
    if stripped.startswith("["):
        close = stripped.find("]")
        if close != -1:
            stripped = stripped[1:close]
    elif ":" in stripped:
        parts = stripped.rsplit(":", 1)
        if len(parts) == 2 and "." in parts[0]:
            stripped = parts[0]
    if "%" in stripped:
        stripped = stripped.split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(stripped))
    except ValueError:
        return None


def client_ip_from_meta(meta: Mapping) -> str | None:
    """Trusted client IP from a WSGI/ASGI ``META``-style mapping."""
    return parse_ip(meta.get("HTTP_X_REAL_IP")) or parse_ip(meta.get("REMOTE_ADDR"))


def client_ip(request) -> str | None:
    """Trusted client IP of a Django/DRF request (``None`` for no request)."""
    if request is None:
        return None
    return client_ip_from_meta(request.META)


def ws_client_ip(scope) -> str | None:
    """Trusted client IP of a Channels scope (``x-real-ip`` header, else scope client)."""
    for name, value in scope.get("headers") or ():
        if name.lower() == b"x-real-ip":
            ip = parse_ip(value.decode("latin-1"))
            if ip is not None:
                return ip
            break
    client = scope.get("client") or (None, None)
    return parse_ip(client[0])
