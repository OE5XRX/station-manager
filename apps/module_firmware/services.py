"""Operator-facing module state transitions (registration + lifecycle).

Kept separate from ingest.py: these are user-driven, audited with the acting
user, and always allowed (including sticky lifecycle states).
"""

import logging

from django.db import transaction

from apps.stations.models import StationAuditLog

from .models import Module, ModuleAssignmentHistory, ModuleFirmwareConvergenceState
from .nudge import nudge_stations, refresh_lock_status
from .reconciler import effective_target, reconcile_module

logger = logging.getLogger(__name__)


@transaction.atomic
def confirm_registration(module, *, user, ip_address=None, audit_suffix=""):
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
        message=f"Module {locked.uid} registration confirmed.{audit_suffix}",
        ip_address=ip_address,
    )


# Operator-settable lifecycle states: every choice except the auto-derived
# ``deployed`` (which is owned by assignment ingestion).
OPERATOR_LIFECYCLE_VALUES = frozenset(Module.Lifecycle.values) - {Module.Lifecycle.DEPLOYED}


@transaction.atomic
def set_lifecycle(module, status, *, user, ip_address=None, audit_suffix=""):
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
        message=f"Lifecycle {old} → {status} for module {locked.uid}.{audit_suffix}",
        changes={"lifecycle_status": {"old": old, "new": status}},
        ip_address=ip_address,
    )


def reconcile_module_type(module_type):
    """Reconcile every module of ``module_type`` that has an open assignment.

    Per-module failures are logged, not raised, so one bad module can't block
    the rest. Stations of modules left UPDATING are nudged to re-check; every
    reconciled station gets a ``control.lock`` refresh (its drain may have ended,
    e.g. target deleted/archived, which sends no nudge; harmlessly redundant with
    the agent consumer's nudge handler for nudged stations, but also reaches
    panels of stations whose agent is offline).
    Returns the number reconciled without error."""
    ok = 0
    to_nudge = set()
    reconciled = set()
    open_assignments = dict(
        ModuleAssignmentHistory.objects.filter(
            to_ts__isnull=True, module__module_type=module_type
        ).values_list("module_id", "station_id")
    )
    for module in Module.objects.filter(module_type=module_type, pk__in=open_assignments):
        try:
            state = reconcile_module(module)
            ok += 1
        except Exception:
            logger.warning("reconcile_module_type: module %s failed", module.pk, exc_info=True)
            continue
        reconciled.add(open_assignments[module.pk])
        if state is not None and state.state == ModuleFirmwareConvergenceState.State.UPDATING:
            to_nudge.add(open_assignments[module.pk])
    nudge_stations(to_nudge)
    refresh_lock_status(reconciled)
    return ok


_UNSET = object()


def quarantined_states_qs():
    """All QUARANTINED convergence rows, latest first (the GUI's fallback order)."""
    return (
        ModuleFirmwareConvergenceState.objects.filter(
            state=ModuleFirmwareConvergenceState.State.QUARANTINED
        )
        .select_related("target_release")
        .order_by("-updated_at", "-pk")
    )


def _open_assignment_target(module):
    assignment = (
        ModuleAssignmentHistory.objects.filter(module=module, to_ts__isnull=True)
        .select_related("station")
        .first()
    )
    if assignment is None or assignment.station is None:
        return None
    return effective_target(assignment.station, module.module_type)


def current_quarantine(module, *, states=None, target=_UNSET):
    """The single live quarantine of ``module`` — the row the GUI shows AND the
    row ``release_quarantine`` releases (one source of truth).

    None unless the module's rollup is QUARANTINED (older QUARANTINED rows
    survive a fix-by-new-target and must not count). Otherwise the QUARANTINED
    row for the current desired release when one exists, else the latest
    QUARANTINED row (e.g. the module lost its assignment and ingest has not
    reconciled yet).

    ``states``: optional pre-fetched QUARANTINED rows of this module, latest
    first (see ``quarantined_states_qs``). ``target``: optional pre-resolved
    effective ModuleFirmwareTarget (None = no target); resolved from the open
    assignment when omitted."""
    if module.firmware_convergence != Module.Convergence.QUARANTINED:
        return None
    if states is None:
        states = list(quarantined_states_qs().filter(module=module))
    if not states:
        return None
    if target is _UNSET:
        target = _open_assignment_target(module)
    if target is not None:
        for cs in states:
            rel = cs.target_release
            if (
                rel.module_type_id == module.module_type_id
                and rel.variant == module.variant
                and rel.version == target.version
            ):
                return cs
    return states[0]


@transaction.atomic
def release_quarantine(module, *, user, ip_address=None, audit_suffix=""):
    """Release ``module``'s live quarantine (``current_quarantine``) for retry.

    Acts only while the rollup is QUARANTINED and only on the row
    ``current_quarantine`` picks — the very row the GUI shows — so stale
    quarantined rows of old targets stay untouched. Always reconciles after
    releasing so the rollup self-heals (an unassigned module drops out of
    QUARANTINED).

    Lock order module → convergence states (same as the reconciler); the pick
    is re-read inside the transaction under the module lock. Returns the
    released target versions; an empty list means nothing to release (no-op,
    no audit)."""
    locked = Module.objects.select_for_update().get(pk=module.pk)
    # Bring the rollup up to date with the CURRENT target first: a target moved
    # without a reconcile (e.g. station-tag change) must not let the latest-row
    # fallback release a stale quarantine of the old target.
    reconcile_module(locked)
    locked.refresh_from_db(fields=["firmware_convergence"])
    states = list(quarantined_states_qs().filter(module=locked).select_for_update(of=("self",)))
    picked = current_quarantine(locked, states=states)
    if picked is None:
        return []
    picked.state = ModuleFirmwareConvergenceState.State.UPDATING
    picked.attempts = 0
    # Also reset the transient-failure streak: a row can be quarantined with
    # transient_failures >= DRAIN_TRANSIENT_LIMIT (e.g. 5 'failed' then one
    # 'rejected'); leaving it would keep the row out of the drain the release
    # is meant to (re)start.
    picked.transient_failures = 0
    picked.last_error_mode = ""
    picked.last_error_message = ""
    picked.save(
        update_fields=[
            "state",
            "attempts",
            "transient_failures",
            "last_error_mode",
            "last_error_message",
            "updated_at",
        ]
    )
    versions = [picked.target_release.version]
    reconcile_module(locked)
    open_assignment = (
        ModuleAssignmentHistory.objects.filter(module=locked, to_ts__isnull=True)
        .select_related("station")
        .first()
    )
    if open_assignment is not None:
        # The release (re)starts a drain on the station: push a fresh lock frame to
        # open panels and nudge the agent instead of waiting for its next cadence.
        nudge_stations([open_assignment.station_id])
        refresh_lock_status([open_assignment.station_id])
    try:
        with transaction.atomic():
            StationAuditLog.log(
                station=open_assignment.station if open_assignment else None,
                module=locked,
                event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED,
                message=(
                    f"Quarantine released for module {locked.uid}: "
                    f"{', '.join(versions)}.{audit_suffix}"
                ),
                changes={"released_targets": versions},
                user=user,
                ip_address=ip_address,
            )
    except Exception:
        logger.warning("release_quarantine: audit write failed", exc_info=True)
    module.firmware_convergence = locked.firmware_convergence
    return versions
