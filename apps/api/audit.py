"""Audit wiring for API write actions — every mutation records token origin.

Best-effort: a transient audit-table failure must never 500 a succeeded
mutation, but the call is mandatory on every write path (spec §Cross-cutting).
"""

import ipaddress
import logging

from apps.accounts.models import AccountAuditLog
from apps.stations.models import StationAuditLog

logger = logging.getLogger(__name__)


def _token_suffix(request):
    token = getattr(request, "auth", None)
    prefix = getattr(token, "prefix", None)
    return f" via API token {prefix}" if prefix else " via API (session)"


def _parse_ip(candidate):
    """Return the candidate as a normalised IP string, or None if invalid.

    Handles the following non-trivial forms, in addition to plain IPv4/IPv6:

    * ``IPv4:port``          — e.g. ``203.0.113.1:8080``  → ``203.0.113.1``
    * ``[IPv6]:port``        — e.g. ``[::1]:8080``         → ``::1``
    * ``IPv6%zone``          — e.g. ``fe80::1%eth0``        → ``fe80::1``

    The parsed (normalised) string is returned so scope IDs and leading zeros
    are always stripped — matching what GenericIPAddressField stores.
    Garbage input returns ``None``; invalid input must never abort an audit.
    """
    if not candidate:
        return None

    stripped = candidate

    # Bracketed IPv6 with optional port: "[addr]:port" or just "[addr]"
    if stripped.startswith("["):
        # Extract address between the brackets
        close = stripped.find("]")
        if close != -1:
            stripped = stripped[1:close]
        # else malformed bracket — fall through and let ip_address() reject it

    # IPv4:port (exactly one colon, left part contains a dot → IPv4 address)
    elif ":" in stripped:
        parts = stripped.rsplit(":", 1)
        if len(parts) == 2 and "." in parts[0]:
            stripped = parts[0]
        # Otherwise it is a plain IPv6 address (multiple colons) — keep as-is.

    # Strip IPv6 zone/scope ID (e.g. "fe80::1%eth0") — ipaddress rejects these.
    if "%" in stripped:
        stripped = stripped.split("%", 1)[0]

    try:
        return str(ipaddress.ip_address(stripped))
    except ValueError:
        return None


def _client_ip(request):
    """Return the client IP string, or None if it cannot be validated.

    Validates each candidate with ipaddress.ip_address() so that a
    request-controlled X-Forwarded-For value can never write garbage into
    the GenericIPAddressField and trigger a DB error that the broad
    audit except-block would swallow (audit-row suppression via bad XFF).
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        candidate = xff.split(",")[0].strip()
        ip = _parse_ip(candidate)
        if ip is not None:
            return ip
    # Fall back to REMOTE_ADDR; also validate it.
    return _parse_ip(request.META.get("REMOTE_ADDR"))


def audit_station_write(request, *, station=None, station_id=None, event_type, message):
    """Write a StationAuditLog entry for an API write operation.

    Pass either ``station`` (instance) or ``station_id`` (pk).  The
    ``station_id`` form is required when auditing a delete because
    ``instance.pk`` is cleared to None by Django after a successful
    ``instance.delete()`` call.
    """
    try:
        StationAuditLog.log(
            station=station,
            station_id=station_id,
            event_type=event_type,
            message=message + _token_suffix(request),
            user=request.user,
            ip_address=_client_ip(request),
        )
    except Exception:  # pragma: no cover - audit must never break the mutation
        logger.warning("API audit (station) write failed", exc_info=True)


def audit_account_write(request, *, event_type, target_user=None, region=None, message=""):
    try:
        AccountAuditLog.log(
            event_type=event_type,
            actor=request.user,
            target_user=target_user,
            region=region,
            message=message + _token_suffix(request),
            ip_address=_client_ip(request),
        )
    except Exception:  # pragma: no cover
        logger.warning("API audit (account) write failed", exc_info=True)


def audit_config_write(request, *, message):
    """Audit a subject-less global config/taxonomy write (StationTag, rollout
    sequences, alert rules). No station/region FK, so it records to
    AccountAuditLog with the CONFIG_CHANGED event type."""
    audit_account_write(
        request, event_type=AccountAuditLog.EventType.CONFIG_CHANGED, message=message
    )
