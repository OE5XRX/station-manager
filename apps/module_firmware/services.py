"""Operator-facing module state transitions (registration + lifecycle).

Kept separate from ingest.py: these are user-driven, audited with the acting
user, and always allowed (including sticky lifecycle states).
"""

import logging

from django.db import transaction

from apps.stations.models import StationAuditLog

from .models import Module, ModuleAssignmentHistory, ModuleFirmwareConvergenceState
from .reconciler import reconcile_module

logger = logging.getLogger(__name__)


@transaction.atomic
def confirm_registration(module, *, user):
    """Mark a module as registered. Idempotent — no-op if already registered.

    Reloads the module under ``select_for_update`` so two concurrent
    confirmations can't both pass the no-op check and emit duplicate audits."""
    locked = Module.objects.select_for_update().get(pk=module.pk)
    if locked.registration_status == Module.Registration.REGISTERED:
        return
    locked.registration_status = Module.Registration.REGISTERED
    locked.save(update_fields=["registration_status", "updated_at"])
    module.registration_status = locked.registration_status
    StationAuditLog.log(
        module=locked,
        user=user,
        event_type=StationAuditLog.EventType.MODULE_REGISTERED,
        message=f"Module {locked.uid} registration confirmed.",
    )


# Operator-settable lifecycle states: every choice except the auto-derived
# ``deployed`` (which is owned by assignment ingestion).
OPERATOR_LIFECYCLE_VALUES = frozenset(Module.Lifecycle.values) - {Module.Lifecycle.DEPLOYED}


@transaction.atomic
def set_lifecycle(module, status, *, user):
    """Set the lifecycle status (operator override, sticky states allowed).

    Validates ``status`` against the operator-settable choices — ``deployed`` is
    assignment-derived and arbitrary strings are rejected, so no caller can
    bypass the invariant. Reloads under ``select_for_update`` so concurrent sets
    can't both log."""
    if status not in OPERATOR_LIFECYCLE_VALUES:
        raise ValueError(f"{status!r} is not an operator-settable lifecycle status")
    locked = Module.objects.select_for_update().get(pk=module.pk)
    # `ready` is assignment-derived (a module with an open assignment is
    # `deployed`). Setting `ready` by hand while an assignment is open would put
    # the module in a state that contradicts its location until the next ingest
    # flips it back — reject it. Operators pull the module (which closes the
    # assignment and reconciles to `ready`) or set a sticky state instead.
    if status == Module.Lifecycle.READY and locked.assignments.filter(to_ts__isnull=True).exists():
        raise ValueError("cannot set 'ready' while the module has an open assignment")
    if locked.lifecycle_status == status:
        return
    old = locked.lifecycle_status
    locked.lifecycle_status = status
    locked.save(update_fields=["lifecycle_status", "updated_at"])
    module.lifecycle_status = status
    StationAuditLog.log(
        module=locked,
        user=user,
        event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED,
        message=f"Lifecycle {old} → {status} for module {locked.uid}.",
        changes={"lifecycle_status": {"old": old, "new": status}},
    )


def reconcile_module_type(module_type):
    """Reconcile every module of ``module_type`` that has an open assignment.

    Per-module failures are logged, not raised, so one bad module can't block
    the rest. Returns the number reconciled without error."""
    ok = 0
    open_ids = ModuleAssignmentHistory.objects.filter(to_ts__isnull=True).values("module_id")
    for module in Module.objects.filter(module_type=module_type, pk__in=open_ids):
        try:
            reconcile_module(module)
            ok += 1
        except Exception:
            logger.warning("reconcile_module_type: module %s failed", module.pk, exc_info=True)
    return ok


@transaction.atomic
def release_quarantine(module, *, user, ip_address=None):
    """Release all quarantined convergence states of ``module`` for retry.

    Lock order module → convergence states (same as the reconciler). Returns
    the released target versions; an empty list means nothing was quarantined
    (no-op, no audit)."""
    locked = Module.objects.select_for_update().get(pk=module.pk)
    states = list(
        ModuleFirmwareConvergenceState.objects.select_for_update()
        .filter(module=locked, state=ModuleFirmwareConvergenceState.State.QUARANTINED)
        .select_related("target_release")
    )
    if not states:
        return []
    versions = []
    for cs in states:
        cs.state = ModuleFirmwareConvergenceState.State.UPDATING
        cs.attempts = 0
        cs.last_error_mode = ""
        cs.last_error_message = ""
        cs.save(
            update_fields=[
                "state",
                "attempts",
                "last_error_mode",
                "last_error_message",
                "updated_at",
            ]
        )
        versions.append(cs.target_release.version)
    reconcile_module(locked)
    open_assignment = (
        ModuleAssignmentHistory.objects.filter(module=locked, to_ts__isnull=True)
        .select_related("station")
        .first()
    )
    try:
        with transaction.atomic():
            StationAuditLog.log(
                station=open_assignment.station if open_assignment else None,
                module=locked,
                event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED,
                message=f"Quarantine released for module {locked.uid}: {', '.join(versions)}.",
                changes={"released_targets": versions},
                user=user,
                ip_address=ip_address,
            )
    except Exception:
        logger.warning("release_quarantine: audit write failed", exc_info=True)
    module.firmware_convergence = locked.firmware_convergence
    return versions
