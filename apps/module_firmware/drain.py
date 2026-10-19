"""Drain predicate for module-firmware flashing (Teilbereich D).

A station "drains" while at least one of its modules has a pending firmware
update that the agent can actually perform: new ControlLock grants are refused
until the update is through, so the flash eventually finds an idle station.

Drain only applies when convergence is possible (spec "Drain nur, wenn
Konvergenz möglich ist"): the agent reports a matching ``module_ota`` backend,
the desired release carries the artifact that backend needs, and the station is
online. Old agents or missing sim artifacts therefore never lock a station.

Everything here is read-only: no ``reconcile_module`` call, no row locks. It
relies on the convergence rows that heartbeat ingestion / the reconcile check
already maintain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone

from .models import (
    DRAIN_TRANSIENT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
)
from .reconciler import desired_release_for_module

ARTIFACT_DFU = "dfu"
ARTIFACT_SIM = "sim"


def artifact_for(module) -> str:
    """Artifact kind a module is flashed with: sim modules get the native_sim
    binary, real hardware the signed DFU image. Never mixed."""
    if module.uid_source == Module.UidSource.SYNTHETIC:
        return ARTIFACT_SIM
    return ARTIFACT_DFU


def artifact_available(release, artifact: str) -> bool:
    """Whether ``release`` carries the given artifact kind."""
    if artifact == ARTIFACT_DFU:
        return bool(release.storage_key)
    if artifact == ARTIFACT_SIM:
        return release.has_sim_artifact
    return False


def executable_artifact(module, release, backends) -> str | None:
    """The artifact kind the agent would flash ``module`` with to reach ``release``,
    or None when the agent cannot execute it (backend not reported, or the release
    lacks that artifact).

    The ONE predicate shared by the drain and the reconcile check: if they drifted
    apart, a station could drain for an update the check never hands out.
    """
    if release is None:
        return None
    artifact = artifact_for(module)
    if artifact not in backends or not artifact_available(release, artifact):
        return None
    return artifact


def pending_updates(station) -> list[ModuleFirmwareConvergenceState]:
    """UPDATING convergence rows of ``station`` the agent can converge, in slot order.

    A row counts only if its module is open-assigned to the station, its target
    is still the module's desired release (a stale row for a superseded target
    is ignored), the module's artifact kind is among the agent's reported
    backends and the desired release carries that artifact. Quarantined / OK
    rows never count, nor rows that hit ``DRAIN_TRANSIENT_LIMIT`` consecutive
    transient failures (convergence evidently not possible right now; the check
    still offers the instruction, only the lock-out ends).
    """
    backends = station.module_ota_backends()
    if not backends:
        return []

    assignments = (
        ModuleAssignmentHistory.objects.filter(station=station, to_ts__isnull=True)
        .select_related("module", "module__module_type")
        .order_by("slot")
    )
    pending = []
    for a in assignments:
        module = a.module
        if artifact_for(module) not in backends:
            continue  # cheap pre-check before resolving the desired release
        desired = desired_release_for_module(module)
        if executable_artifact(module, desired, backends) is None:
            continue
        cs = (
            ModuleFirmwareConvergenceState.objects.filter(
                module=module,
                target_release=desired,
                state=ModuleFirmwareConvergenceState.State.UPDATING,
                transient_failures__lt=DRAIN_TRANSIENT_LIMIT,
            )
            .select_related("module", "target_release")
            .first()
        )
        if cs is not None:
            pending.append(cs)
    return pending


@dataclass(frozen=True)
class DrainStatus:
    active: bool
    since: datetime | None
    module_uids: tuple[str, ...]


_INACTIVE = DrainStatus(active=False, since=None, module_uids=())


def drain_status(station) -> DrainStatus:
    """Drain state of ``station``.

    ``since`` is the earliest ``created_at`` of the pending convergence rows.
    That is an approximation of "waiting for a firmware update since": the row
    is created when the drift is first observed (heartbeat ingestion / check),
    not when the capability or artifact became available. It is good enough for
    the "Station wartet auf Firmware-Update seit …" display.
    """
    if not station.is_online:
        return _INACTIVE
    pending = pending_updates(station)
    if not pending:
        return _INACTIVE
    return DrainStatus(
        active=True,
        since=min(cs.created_at for cs in pending),
        module_uids=tuple(cs.module.uid for cs in pending),
    )


def is_draining(station) -> bool:
    return drain_status(station).active


# --- Dashboard visibility (spec: "Sichtbarkeit im UI/Dashboard … Pflicht") ----------

# Prefix of the agent's DFU-stuck error (station_agent.module_ota.DFU_STUCK_MSG); a
# test pins the two together.
DFU_STUCK_PREFIX = "Modul hängt im DFU-Modus"

# Mirrors Station.is_online (heartbeat < 2 min) so offline stations are never candidates.
_ONLINE_WINDOW = timedelta(seconds=120)


def draining_stations() -> list[tuple[object, DrainStatus]]:
    """Online stations whose drain is active, longest waiting first.

    Cheap by construction: only stations with an open assignment of a module that
    has an UPDATING convergence row below the transient limit are candidates (one
    query); the full ``drain_status`` runs only for those.
    """
    from apps.stations.models import Station

    candidate_ids = (
        ModuleAssignmentHistory.objects.filter(
            to_ts__isnull=True,
            module__convergence_states__state=ModuleFirmwareConvergenceState.State.UPDATING,
            module__convergence_states__transient_failures__lt=DRAIN_TRANSIENT_LIMIT,
        )
        .values("station_id")
        .distinct()
    )
    stations = Station.objects.filter(
        pk__in=candidate_ids, last_seen__gte=timezone.now() - _ONLINE_WINDOW
    )
    result = []
    for station in stations:
        status = drain_status(station)
        if status.active:
            result.append((station, status))
    result.sort(key=lambda pair: (pair[1].since, pair[0].name))
    return result


@dataclass(frozen=True)
class DfuStuckModule:
    module: Module
    station: object | None
    slot: str | None
    since: datetime
    message: str


DFU_STUCK_LIMIT = 50


def dfu_stuck_modules(limit: int = DFU_STUCK_LIMIT) -> list[DfuStuckModule]:
    """Modules whose latest convergence row ended DFU-stuck (power-cycle needed),
    newest first, at most ``limit`` (bounded dashboard query).

    Only the module's most recently updated row counts (a newer target supersedes
    the old error) and converged (OK) rows never count. Quarantined rows do: the
    module still sits in DFU mode until someone power-cycles it.
    """
    newer = ModuleFirmwareConvergenceState.objects.filter(
        module=OuterRef("module"), updated_at__gt=OuterRef("updated_at")
    )
    rows = (
        ModuleFirmwareConvergenceState.objects.filter(
            last_error_message__startswith=DFU_STUCK_PREFIX
        )
        .exclude(state=ModuleFirmwareConvergenceState.State.OK)
        .annotate(superseded=Exists(newer))
        .filter(superseded=False)
        .select_related("module", "module__module_type")
        .prefetch_related(
            Prefetch(
                "module__assignments",
                queryset=ModuleAssignmentHistory.objects.filter(to_ts__isnull=True).select_related(
                    "station"
                ),
                to_attr="open_assignments",
            )
        )
        .order_by("-updated_at")[:limit]
    )
    entries = []
    for cs in rows:
        open_assignment = next(iter(cs.module.open_assignments), None)
        entries.append(
            DfuStuckModule(
                module=cs.module,
                station=open_assignment.station if open_assignment else None,
                slot=open_assignment.slot if open_assignment else None,
                since=cs.updated_at,
                message=cs.last_error_message,
            )
        )
    return entries
