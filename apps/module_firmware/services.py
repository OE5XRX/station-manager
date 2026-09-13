"""Operator-facing module state transitions (registration + lifecycle).

Kept separate from ingest.py: these are user-driven, audited with the acting
user, and always allowed (including sticky lifecycle states).
"""

from apps.stations.models import StationAuditLog

from .models import Module


def confirm_registration(module, *, user):
    """Mark a module as registered. Idempotent — no-op if already registered."""
    if module.registration_status == Module.Registration.REGISTERED:
        return
    module.registration_status = Module.Registration.REGISTERED
    module.save(update_fields=["registration_status", "updated_at"])
    StationAuditLog.log(
        module=module, user=user,
        event_type=StationAuditLog.EventType.MODULE_REGISTERED,
        message=f"Module {module.uid} registration confirmed.",
    )


def set_lifecycle(module, status, *, user):
    """Set the lifecycle status (operator override, sticky states allowed)."""
    if module.lifecycle_status == status:
        return
    old = module.lifecycle_status
    module.lifecycle_status = status
    module.save(update_fields=["lifecycle_status", "updated_at"])
    StationAuditLog.log(
        module=module, user=user,
        event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED,
        message=f"Lifecycle {old} → {status} for module {module.uid}.",
        changes={"lifecycle_status": {"old": old, "new": status}},
    )
