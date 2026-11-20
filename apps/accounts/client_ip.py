"""Client IP for audit rows — the single trusted source.

Production is Cloudflare edge -> cloudflared tunnel -> Daphne (``web:8000``,
no nginx, no published port). ``REMOTE_ADDR`` / the ASGI scope client is
therefore the cloudflared container, and cloudflared forwards every client
header verbatim — ``X-Real-IP`` and ``X-Forwarded-For`` are whatever the
client chose to send and must never be trusted. Cloudflare *overwrites*
``CF-Connecting-IP`` at the edge, so that is the one trusted header.

Which header is trusted comes from ``settings.CLIENT_IP_HEADER`` (an HTTP
header name; prod default ``CF-Connecting-IP``). Empty (dev/test) means no
proxy: only ``REMOTE_ADDR`` / the scope client is used and every header is
ignored. A missing or malformed trusted header falls back to the socket peer.

Every value is validated: a malformed header must never write garbage into a
``GenericIPAddressField`` (a DB error there would suppress the audit row).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping

from django.conf import settings


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


def trusted_header() -> str:
    """The configured trusted header name (``""`` = trust no header)."""
    return (getattr(settings, "CLIENT_IP_HEADER", "") or "").strip()


def _meta_key(header: str) -> str:
    """``CF-Connecting-IP`` -> ``HTTP_CF_CONNECTING_IP`` (Django META naming)."""
    return "HTTP_" + header.upper().replace("-", "_")


def client_ip_from_meta(meta: Mapping) -> str | None:
    """Trusted client IP from a WSGI/ASGI ``META``-style mapping."""
    header = trusted_header()
    ip = parse_ip(meta.get(_meta_key(header))) if header else None
    return ip or parse_ip(meta.get("REMOTE_ADDR"))


def client_ip_from_headers(headers: Mapping) -> str | None:
    """Trusted client IP from oauthlib request headers.

    oauthlib's ``extract_headers`` copies Django's ``request.META`` verbatim
    (``HTTP_CF_CONNECTING_IP``, ``REMOTE_ADDR``); the plain HTTP header name
    is accepted too.
    """
    header = trusted_header()
    ip = None
    if header:
        ip = parse_ip(headers.get(_meta_key(header))) or parse_ip(headers.get(header))
    return ip or parse_ip(headers.get("REMOTE_ADDR"))


def client_ip(request) -> str | None:
    """Trusted client IP of a Django/DRF request (``None`` for no request)."""
    if request is None:
        return None
    return client_ip_from_meta(request.META)


def ws_client_ip(scope) -> str | None:
    """Trusted client IP of a Channels scope (trusted header, else scope client)."""
    header = trusted_header().lower().encode("latin-1")
    if header:
        for name, value in scope.get("headers") or ():
            if name.lower() == header:
                ip = parse_ip(value.decode("latin-1"))
                if ip is not None:
                    return ip
                break
    client = scope.get("client") or (None, None)
    return parse_ip(client[0])
