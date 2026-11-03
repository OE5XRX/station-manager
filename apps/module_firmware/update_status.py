"""Central, read-only module firmware update status (spec §4).

UI and API derive the per-module display state ONLY from here. The resolution
mirrors ``reconciler.reconcile_module`` (open assignment -> effective target ->
non-archived release for (type, variant, version) -> convergence row) and the
gates of the reconcile check (``drain.executable_artifact``, ControlLock idle
gate), so the status never disagrees with what ``check`` would hand out.

It also mirrors the reconciler's *decisions* on the stored row fields (boot-key
mismatch -> not updatable, key fixed -> retry, updating row at the transient cap
-> failed), so the display never disagrees with what the next reconcile / check
would do — even for an offline station that does not reconcile.

Pure read: no ``reconcile_module`` call, no row locks, no writes.
``bulk_update_status`` issues a constant number of queries (at most five),
independent of the number of modules.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from operator import or_

from django.db import models
from django.db.models import Q

from apps.control.models import ControlLock

from .drain import artifact_for, executable_artifact
from .mcuboot import KEY_UNKNOWN_HINT, boot_key_mismatch, key_hint_applies, not_updatable_text
from .models import (
    DRAIN_TRANSIENT_LIMIT,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
)
from .reconciler import resolve_effective_target

CS = ModuleFirmwareConvergenceState
_ROW = CS.State
_MODE = CS.ErrorMode


class UpdateState(models.TextChoices):
    """Stable API enum of the display state."""

    NO_TARGET = "no_target"
    CURRENT = "current"
    QUARANTINED = "quarantined"
    NOT_UPDATABLE = "not_updatable"
    FAILED = "failed"
    WAITING = "waiting"
    RUNNING = "running"
    PENDING = "pending"


class WaitReason(models.TextChoices):
    AGENT_NO_MODULE_OTA = "agent_no_module_ota"
    NO_RELEASE_FOR_VARIANT = "no_release_for_variant"
    NO_ARTIFACT = "no_artifact"
    STATION_OFFLINE = "station_offline"
    STATION_BUSY = "station_busy"


# reason_code enum for the API: wait reasons, stored row reasons, quarantine modes.
REASON_CODES: tuple[str, ...] = (
    *WaitReason.values,
    *CS.ReasonCode.values,
    _MODE.REJECTED.value,
    _MODE.ROLLED_BACK.value,
)

_LIVE = frozenset(
    {UpdateState.RUNNING.value, UpdateState.WAITING.value, UpdateState.PENDING.value}
)

_SHORT_LABELS = {
    UpdateState.NO_TARGET.value: "Kein Ziel",
    UpdateState.CURRENT.value: "Aktuell",
    UpdateState.QUARANTINED.value: "Quarantäne",
    UpdateState.NOT_UPDATABLE.value: "Nicht updatebar",
    UpdateState.FAILED.value: "Fehler",
    UpdateState.WAITING.value: "Wartet",
    UpdateState.RUNNING.value: "Update läuft",
    UpdateState.PENDING.value: "Update ausstehend",
}

_PHASE_TEXT = {
    CS.Phase.DOWNLOADING.value: "Download",
    CS.Phase.FLASHING.value: "Flashen",
    CS.Phase.VERIFYING.value: "Prüfen",
}

_WAIT_TEXT = {
    WaitReason.STATION_OFFLINE.value: "Wartet: Station offline",
    WaitReason.AGENT_NO_MODULE_OTA.value: (
        "Wartet: Station-Agent kann keine Module flashen (Image zu alt)"
    ),
    WaitReason.STATION_BUSY.value: "Wartet: Station belegt (Bedienung aktiv)",
}


@dataclass(frozen=True)
class UpdateStatus:
    state: str  # UpdateState
    phase: str  # "" or Phase value
    reason_code: str  # "" or one of REASON_CODES
    reason_text: str  # full German display line
    since: datetime | None

    def __post_init__(self):
        # Plain str values (not TextChoices members) for a stable API surface.
        for name in ("state", "phase", "reason_code"):
            value = getattr(self, name)
            object.__setattr__(self, name, str(getattr(value, "value", value)))

    @property
    def is_live(self) -> bool:
        """Whether the state can still change on its own (UI keeps polling)."""
        return self.state in _LIVE

    @property
    def short_label(self) -> str:
        return _SHORT_LABELS.get(self.state, "")


_NO_TARGET = UpdateStatus(UpdateState.NO_TARGET, "", "", "Kein Ziel", None)
_CURRENT = UpdateStatus(UpdateState.CURRENT, "", "", "Aktuell", None)


def _waiting(reason: str, text: str | None = None) -> UpdateStatus:
    return UpdateStatus(UpdateState.WAITING, "", reason, text or _WAIT_TEXT[str(reason)], None)


def _quarantine_status(module, release, row) -> UpdateStatus:
    mode = row.last_error_mode
    if mode == _MODE.REJECTED:
        text = "Quarantäne (vom Modul abgelehnt)"
    else:
        detail = f"Update {row.attempts}× zurückgerollt"
        if key_hint_applies(module, release, mode):
            detail += f" – {KEY_UNKNOWN_HINT}"
        text = f"Quarantäne ({detail})"
    code = mode if mode in REASON_CODES else ""
    return UpdateStatus(UpdateState.QUARANTINED, "", code, text, row.updated_at)


def _decide(module, station, target, release, row, busy_station_ids) -> UpdateStatus:
    """Priority table of spec §4 — first match wins."""
    if station is None or target is None:
        return _NO_TARGET
    if (module.last_reported_version or "") == target.version:
        return _CURRENT

    # Mirror reconciler._decide (§5.2/§5.3) on the row fields so the display
    # never disagrees with what the next reconcile / check would decide — even
    # for a station that is offline and therefore never reconciles.
    since = row.updated_at if row is not None else None
    if release is not None and boot_key_mismatch(module, release):
        # Known, differing keys override updating/failed/quarantined.
        return UpdateStatus(
            UpdateState.NOT_UPDATABLE,
            "",
            CS.ReasonCode.BOOT_KEY_MISMATCH,
            not_updatable_text(module),
            since,
        )

    state = row.state if row is not None else None
    released = (
        state == _ROW.NOT_UPDATABLE
        and bool(module.reported_boot_key)
        and module.reported_boot_key == release.boot_key_hash
    )
    if released:
        state = _ROW.UPDATING  # key fixed: the reconciler retries with fresh counters

    if state == _ROW.QUARANTINED:
        return _quarantine_status(module, release, row)
    if state == _ROW.NOT_UPDATABLE:
        return UpdateStatus(
            UpdateState.NOT_UPDATABLE,
            "",
            CS.ReasonCode.BOOT_KEY_MISMATCH,
            not_updatable_text(module),
            since,
        )
    at_cap = (
        state == _ROW.UPDATING and not released and row.transient_failures >= DRAIN_TRANSIENT_LIMIT
    )
    if state == _ROW.FAILED or at_cap:
        message = row.last_error_message or "Update mehrfach fehlgeschlagen"
        return UpdateStatus(
            UpdateState.FAILED, "", CS.ReasonCode.TRANSIENT_LIMIT, f"Fehler: {message}", since
        )

    if not station.is_online:
        return _waiting(WaitReason.STATION_OFFLINE)

    if row is not None and row.phase:
        label = _PHASE_TEXT.get(row.phase, row.phase)
        return UpdateStatus(
            UpdateState.RUNNING, row.phase, "", f"Update läuft: {label}", row.phase_at
        )

    backends = station.module_ota_backends()
    artifact = artifact_for(module)
    if artifact not in backends:
        return _waiting(WaitReason.AGENT_NO_MODULE_OTA)
    if release is None:
        return _waiting(
            WaitReason.NO_RELEASE_FOR_VARIANT,
            f"Wartet: Keine Firmware {target.version} für Variante "
            f"{module.variant or '–'} vorhanden",
        )
    if executable_artifact(module, release, backends) is None:
        return _waiting(
            WaitReason.NO_ARTIFACT, f"Wartet: Release enthält kein passendes Artefakt ({artifact})"
        )
    if station.pk in busy_station_ids:
        return _waiting(WaitReason.STATION_BUSY)

    return UpdateStatus(
        UpdateState.PENDING, "", "", "Update ausstehend", row.created_at if row else None
    )


def _open_stations(modules) -> dict[int, object]:
    """module pk -> station of its open assignment (or None). Uses an
    ``open_assignments`` prefetch when present; one query for the rest."""
    stations: dict[int, object] = {}
    missing = []
    for m in modules:
        prefetched = getattr(m, "open_assignments", None)
        if prefetched is None:
            missing.append(m.pk)
            continue
        a = next((a for a in prefetched if a.to_ts is None), None)
        stations[m.pk] = a.station if a is not None else None
    if missing:
        for a in ModuleAssignmentHistory.objects.filter(
            module_id__in=missing, to_ts__isnull=True
        ).select_related("station"):
            stations[a.module_id] = a.station
    return stations


def _resolve(modules: Sequence, station_override=None) -> dict[int, UpdateStatus]:
    modules = [m for m in modules if m is not None]
    if not modules:
        return {}

    if station_override is not None:
        stations = {m.pk: station_override for m in modules}
    else:
        stations = _open_stations(modules)

    # Effective targets — one query over all involved module types.
    type_ids = {m.module_type_id for m in modules if stations.get(m.pk) is not None}
    targets_by_type = defaultdict(list)
    if type_ids:
        for t in ModuleFirmwareTarget.objects.filter(module_type_id__in=type_ids):
            targets_by_type[t.module_type_id].append(t)
    targets = {}
    for m in modules:
        station = stations.get(m.pk)
        if station is not None:
            targets[m.pk] = resolve_effective_target(station, targets_by_type[m.module_type_id])

    # Desired releases — same lookup as reconcile_module (default manager hides
    # archived rows; (type, variant, version) is unique).
    keys = {
        (m.module_type_id, m.variant, targets[m.pk].version)
        for m in modules
        if targets.get(m.pk) is not None
    }
    releases = {}
    if keys:
        q = reduce(or_, (Q(module_type_id=t, variant=v, version=ver) for t, v, ver in keys))
        for r in ModuleFirmwareRelease.objects.filter(q):
            releases[(r.module_type_id, r.variant, r.version)] = r
    desired = {
        m.pk: releases.get((m.module_type_id, m.variant, targets[m.pk].version))
        for m in modules
        if targets.get(m.pk) is not None
    }

    # Convergence rows for (module, desired release).
    rows = {}
    pairs = {(pk, r.pk) for pk, r in desired.items() if r is not None}
    if pairs:
        for cs in CS.objects.filter(
            module_id__in={p[0] for p in pairs},
            target_release_id__in={p[1] for p in pairs},
        ):
            if (cs.module_id, cs.target_release_id) in pairs:
                rows[cs.module_id] = cs

    # Busy stations (ControlLock held, any scope — same as the check's idle gate).
    station_ids = {stations[pk].pk for pk in targets if targets[pk] is not None}
    busy = set()
    if station_ids:
        busy = set(
            ControlLock.objects.filter(
                station_id__in=station_ids, holder__isnull=False
            ).values_list("station_id", flat=True)
        )

    return {
        m.pk: _decide(
            m,
            stations.get(m.pk),
            targets.get(m.pk),
            desired.get(m.pk),
            rows.get(m.pk),
            busy,
        )
        for m in modules
    }


def bulk_update_status(modules: Sequence, *, station=None) -> dict[int, UpdateStatus]:
    """Update status for many modules, keyed by module pk. Constant query count
    (≤ 5): open assignments + stations (skipped with an ``open_assignments``
    prefetch or when ``station`` is given), targets, releases, convergence rows,
    busy station ids. ``station`` replaces every module's open-assignment station
    (e.g. the station card, whose modules all sit on that station)."""
    return _resolve(modules, station_override=station)


def module_update_status(module, *, station=None) -> UpdateStatus:
    """Update status of one module. ``station`` (when given) replaces the
    open-assignment lookup — e.g. the station card already has it."""
    return _resolve([module], station_override=station)[module.pk]
