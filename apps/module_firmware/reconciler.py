"""Pure desired/actual resolution + convergence bookkeeping for module
firmware. No HTTP, no request objects — callable from ingestion, target
mutation, and the reconcile API. State is always derived from the real
reported version, never from local flags."""

import logging

from django.db import transaction
from django.utils import timezone

from apps.stations.models import StationAuditLog

from .models import (
    QUARANTINE_ATTEMPT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
)

logger = logging.getLogger(__name__)


def effective_target(station, module_type):
    """Resolve the effective ModuleFirmwareTarget for (station, module_type)
    with precedence station > tag > fleet, honouring the canary gate.

    Returns None when nothing applies (incl. a fleet target whose canary_tag
    excludes this station)."""
    station_t = ModuleFirmwareTarget.objects.filter(
        module_type=module_type, scope=ModuleFirmwareTarget.Scope.STATION, station=station
    ).first()
    if station_t is not None:
        return station_t

    station_tag_ids = list(station.tags.values_list("id", flat=True))
    if station_tag_ids:
        tag_t = (
            ModuleFirmwareTarget.objects.filter(
                module_type=module_type,
                scope=ModuleFirmwareTarget.Scope.TAG,
                tag_id__in=station_tag_ids,
            )
            .order_by("-updated_at")
            .first()
        )
        if tag_t is not None:
            return tag_t

    fleet_t = ModuleFirmwareTarget.objects.filter(
        module_type=module_type, scope=ModuleFirmwareTarget.Scope.FLEET
    ).first()
    if fleet_t is None:
        return None
    # Canary gate: while canary_tag is set, a fleet target applies only to
    # stations carrying that tag. Non-canary stations get no target — no drift.
    if fleet_t.canary_tag_id is not None and fleet_t.canary_tag_id not in station_tag_ids:
        return None
    return fleet_t


def desired_release_for_module(module):
    """Resolve the concrete, non-archived ModuleFirmwareRelease for a module:
    its open-assignment station + module_type + variant -> effective target
    version -> the release matching (module_type, variant, version).

    Type AND variant gate: no release for the variant -> None (no flash,
    drift stays visible)."""
    assignment = (
        ModuleAssignmentHistory.objects.filter(module=module, to_ts__isnull=True)
        .select_related("station")
        .first()
    )
    if assignment is None or assignment.station is None:
        return None
    target = effective_target(assignment.station, module.module_type)
    if target is None:
        return None
    return ModuleFirmwareRelease.objects.filter(
        module_type=module.module_type,
        variant=module.variant,
        version=target.version,
    ).first()


def _set_convergence_rollup(module, value):
    """Denormalized Module.firmware_convergence — cheap read for D/dashboard."""
    if module.firmware_convergence != value:
        module.firmware_convergence = value
        module.save(update_fields=["firmware_convergence", "updated_at"])


@transaction.atomic
def reconcile_module(module):
    """Compare reported vs desired, upsert the ConvergenceState for the current
    target release, and maintain the Module.firmware_convergence rollup.

    Idempotent. Returns the active ConvergenceState, or None when there is no
    desired release (no drift; rollup falls back to ``ok`` if the module is
    running something, else ``unknown``)."""
    desired = desired_release_for_module(module)
    if desired is None:
        # No target/variant match => no drift. Keep unknown unless we already
        # know it is running a version (then it is trivially ok w.r.t. intent).
        _set_convergence_rollup(
            module,
            Module.Convergence.UNKNOWN
            if module.firmware_convergence == Module.Convergence.UNKNOWN
            else Module.Convergence.OK,
        )
        return None

    cs, _created = ModuleFirmwareConvergenceState.objects.get_or_create(
        module=module, target_release=desired
    )

    # A quarantined row for the still-current target stays quarantined — never
    # auto-reactivated. A fix is a NEW target release => a different row.
    if cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
        _set_convergence_rollup(module, Module.Convergence.QUARANTINED)
        return cs

    reported = module.last_reported_version or ""
    if reported == desired.version:
        cs.state = ModuleFirmwareConvergenceState.State.OK
        cs.save(update_fields=["state", "updated_at"])
        _set_convergence_rollup(module, Module.Convergence.OK)
    else:
        cs.state = ModuleFirmwareConvergenceState.State.UPDATING
        cs.save(update_fields=["state", "updated_at"])
        _set_convergence_rollup(module, Module.Convergence.UPDATING)
    return cs


@transaction.atomic
def record_error(convergence, error_mode, error_message=""):
    """Apply a reported failure to a ConvergenceState per the quarantine rules:

    - rejected  -> immediate quarantine, attempts unchanged (retry never helps);
    - rolled_back -> attempts++, quarantine once attempts >= N;
    - transient -> no attempts++, stays updating.

    Stamps last_attempt_at/last_error_mode and maintains the rollup."""
    convergence.last_error_mode = error_mode
    convergence.last_error_message = error_message
    convergence.last_attempt_at = timezone.now()
    fields = ["last_error_mode", "last_error_message", "last_attempt_at", "updated_at"]

    if error_mode == ModuleFirmwareConvergenceState.ErrorMode.REJECTED:
        convergence.state = ModuleFirmwareConvergenceState.State.QUARANTINED
        fields.append("state")
    elif error_mode == ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK:
        convergence.attempts += 1
        fields.append("attempts")
        if convergence.attempts >= QUARANTINE_ATTEMPT_LIMIT:
            convergence.state = ModuleFirmwareConvergenceState.State.QUARANTINED
            fields.append("state")
    # transient: no attempts change, state stays updating.

    convergence.save(update_fields=fields)

    if convergence.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
        _set_convergence_rollup(convergence.module, Module.Convergence.QUARANTINED)
    else:
        _set_convergence_rollup(convergence.module, Module.Convergence.UPDATING)
    return convergence
