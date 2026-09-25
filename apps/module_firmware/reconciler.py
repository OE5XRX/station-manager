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
