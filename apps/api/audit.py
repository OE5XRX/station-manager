"""Audit wiring for API write actions — every mutation records token origin.

Best-effort: a transient audit-table failure must never 500 a succeeded
mutation, but the call is mandatory on every write path (spec §Cross-cutting).
"""

import logging

from apps.accounts.models import AccountAuditLog
from apps.stations.models import StationAuditLog

logger = logging.getLogger(__name__)


def _token_suffix(request):
    token = getattr(request, "auth", None)
    prefix = getattr(token, "prefix", None)
    return f" via API token {prefix}" if prefix else " via API (session)"


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


def audit_station_write(request, *, station, event_type, message):
    try:
        StationAuditLog.log(
            station=station,
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
