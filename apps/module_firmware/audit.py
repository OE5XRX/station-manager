"""Best-effort audit helpers for the module-firmware GUI views.

A transient audit-table failure must never 500 an already-applied mutation.
"""

import logging

from apps.accounts.client_ip import client_ip
from apps.accounts.models import AccountAuditLog
from apps.stations.models import StationAuditLog

logger = logging.getLogger(__name__)


def audit_config(request, *, obj, action, message, details=None):
    """Subject-less config write (module types, targets) → AccountAuditLog."""
    try:
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
            actor=request.user,
            ip_address=client_ip(request),
            message=message,
            details={"object": obj, "action": action, **(details or {})},
        )
    except Exception:
        logger.warning("module_firmware audit (config) write failed", exc_info=True)


def audit_station(request, *, event_type, message, station=None, module=None, changes=None):
    """Station/module-subject write → StationAuditLog. No-op without a subject."""
    if station is None and module is None:
        return
    try:
        StationAuditLog.log(
            station=station,
            module=module,
            event_type=event_type,
            message=message,
            changes=changes,
            user=request.user,
            ip_address=client_ip(request),
        )
    except Exception:
        logger.warning("module_firmware audit (station) write failed", exc_info=True)
