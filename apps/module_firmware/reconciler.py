"""Pure desired/actual resolution + convergence bookkeeping for module
firmware. No HTTP, no request objects — callable from ingestion, target
mutation, and the reconcile API. State is always derived from the real
reported version, never from local flags."""

import logging

from django.db import transaction
from django.utils import timezone

from apps.stations.models import StationAuditLog

from .mcuboot import KEY_UNKNOWN_HINT, boot_key_mismatch, key_hint_applies, not_updatable_text
from .models import (
    DRAIN_TRANSIENT_LIMIT,
    QUARANTINE_ATTEMPT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
)

logger = logging.getLogger(__name__)

State = ModuleFirmwareConvergenceState.State


def _audit(module, event_type, message, *, station=None):
    """Best-effort dual-subject audit write — never break the state change."""
    try:
        StationAuditLog.log(station=station, module=module, event_type=event_type, message=message)
    except Exception:
        logger.warning("reconciler: audit write failed (%s)", event_type, exc_info=True)


def resolve_effective_target(station, targets):
    """Pure: pick the effective target for ``station`` out of ``targets`` (all of
    one module type) with precedence station > group > fleet, honouring the
    canary gate. No queries — usable for bulk resolution over prefetched rows.

    Returns None when nothing applies (incl. a fleet target whose canary_group
    excludes this station)."""
    group_id = station.rollout_group_id
    station_t = group_t = fleet_t = None
    for t in targets:
        if t.scope == ModuleFirmwareTarget.Scope.STATION:
            if station_t is None and t.station_id == station.pk:
                station_t = t
        elif t.scope == ModuleFirmwareTarget.Scope.GROUP:
            if group_t is None and group_id is not None and t.group_id == group_id:
                group_t = t
        elif t.scope == ModuleFirmwareTarget.Scope.FLEET and fleet_t is None:
            fleet_t = t
    if station_t is not None:
        return station_t
    if group_t is not None:
        return group_t
    if fleet_t is None:
        return None
    # Canary gate: while canary_group is set, a fleet target applies only to
    # stations in that group. Non-canary stations get no target — no drift.
    if fleet_t.canary_group_id is not None and fleet_t.canary_group_id != group_id:
        return None
    return fleet_t


def effective_target(station, module_type):
    """Resolve the effective ModuleFirmwareTarget for (station, module_type) —
    one query + :func:`resolve_effective_target`."""
    return resolve_effective_target(
        station, ModuleFirmwareTarget.objects.filter(module_type=module_type)
    )


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
    # Global lock order module → (assignment) → convergence (see R2-1). Lock the
    # MODULE row FIRST so every path that locks more than one of these rows
    # acquires the module lock before the convergence lock. ``check`` does not
    # pre-lock the module; ingestion already holds it (a same-transaction no-op).
    Module.objects.select_for_update().filter(pk=module.pk).first()

    # The rollup decisions below compare against Module.firmware_convergence; a
    # concurrent record_error/status transaction may have advanced it since this
    # ``module`` object was read, so refresh the denormalized field from the DB
    # to avoid a stale-value no-op that would leave the rollup out of sync.
    module.refresh_from_db(fields=["firmware_convergence"])

    # Resolve the effective target explicitly so we can distinguish "no intent"
    # (no assignment / no target) from "intent exists but no variant-matching
    # release" (variant-gate drift, which must stay VISIBLE — see R2-2).
    assignment = (
        ModuleAssignmentHistory.objects.filter(module=module, to_ts__isnull=True)
        .select_related("station")
        .first()
    )
    target = None
    if assignment is not None and assignment.station is not None:
        target = effective_target(assignment.station, module.module_type)

    if target is None:
        # Truly no intent => no drift. Keep unknown unless we already know it is
        # running a version (then it is trivially ok w.r.t. intent).
        _set_convergence_rollup(
            module,
            Module.Convergence.UNKNOWN
            if module.firmware_convergence == Module.Convergence.UNKNOWN
            else Module.Convergence.OK,
        )
        return None

    desired = ModuleFirmwareRelease.objects.filter(
        module_type=module.module_type,
        variant=module.variant,
        version=target.version,
    ).first()
    if desired is None:
        # Intent EXISTS but there is no variant-matching release: no flash is
        # possible, yet the drift must remain VISIBLE ("Drift bleibt sichtbar").
        # There can be no ConvergenceState row (its target_release FK is
        # non-nullable), so drive the rollup directly off the real reported
        # version vs. the target VERSION: equal => OK (already on desired),
        # otherwise UPDATING (visible drift). Return None — no instruction, since
        # ``check`` has no release to hand out.
        reported = module.last_reported_version or ""
        _set_convergence_rollup(
            module,
            Module.Convergence.OK if reported == target.version else Module.Convergence.UPDATING,
        )
        return None

    cs, _created = ModuleFirmwareConvergenceState.objects.get_or_create(
        module=module, target_release=desired
    )
    # Re-read the row under a row lock before deciding/saving state. Without this
    # a heartbeat-driven reconcile can read a stale ``updating`` while a
    # concurrent status endpoint locks + quarantines, then this transaction would
    # resurrect the quarantined instruction by saving its stale state.
    cs = ModuleFirmwareConvergenceState.objects.select_for_update().get(pk=cs.pk)

    return _decide(cs, module, desired, station=assignment.station)


_STICKY_ROLLUP = {
    State.QUARANTINED: Module.Convergence.QUARANTINED,
    State.NOT_UPDATABLE: Module.Convergence.NOT_UPDATABLE,
    State.FAILED: Module.Convergence.FAILED,
}


def _decide(cs, module, desired, *, station):
    """State decision for a locked row of the current target (spec §5.2/§5.3).
    Order matters: reached version > key mismatch > key fixed > sticky >
    transient cap > updating."""
    reported = module.last_reported_version or ""

    # 1) Reached the desired version: always OK — except quarantine, which is
    #    only ever released explicitly (unchanged behaviour).
    if reported == desired.version and cs.state != State.QUARANTINED:
        cs.state = State.OK
        cs.transient_failures = 0
        cs.reason_code = ""
        cs.phase = ""
        cs.phase_at = None
        cs.instructed_at = None
        cs.save(
            update_fields=[
                "state",
                "transient_failures",
                "reason_code",
                "phase",
                "phase_at",
                "instructed_at",
                "updated_at",
            ]
        )
        _set_convergence_rollup(module, Module.Convergence.OK)
        return cs

    # 2) Both keys known and different: no flash can ever succeed. The precise
    #    diagnosis overrides updating, failed and quarantined. Audit on transition.
    if boot_key_mismatch(module, desired):
        if cs.state != State.NOT_UPDATABLE:
            cs.state = State.NOT_UPDATABLE
            cs.reason_code = ModuleFirmwareConvergenceState.ReasonCode.BOOT_KEY_MISMATCH
            cs.phase = ""
            cs.phase_at = None
            cs.save(update_fields=["state", "reason_code", "phase", "phase_at", "updated_at"])
            _audit(
                module,
                StationAuditLog.EventType.MODULE_NOT_UPDATABLE,
                not_updatable_text(module),
                station=station,
            )
        _set_convergence_rollup(module, Module.Convergence.NOT_UPDATABLE)
        return cs

    # 3) Keys known and equal again (e.g. after a bench reflash): release ONLY
    #    not_updatable, with fresh counters. Falls through to the updating branch.
    released = False
    if (
        cs.state == State.NOT_UPDATABLE
        and module.reported_boot_key
        and module.reported_boot_key == desired.boot_key_hash
    ):
        cs.state = State.UPDATING
        cs.attempts = 0
        cs.transient_failures = 0
        cs.reason_code = ""
        released = True
        _audit(
            module,
            StationAuditLog.EventType.MODULE_UPDATABLE_AGAIN,
            f"Modul {module.uid}: Bootloader-Schlüssel passt wieder zur Release "
            f"(Ziel {desired.version}) – Update wird wieder versucht.",
            station=station,
        )

    # 4) Sticky states: a heartbeat never resets them.
    if cs.state in _STICKY_ROLLUP:
        _set_convergence_rollup(module, _STICKY_ROLLUP[cs.state])
        return cs

    # 5) An updating row already at the transient cap (e.g. migrated, or counted
    #    before this rule existed) gives up now instead of being re-offered.
    if cs.transient_failures >= DRAIN_TRANSIENT_LIMIT:
        _give_up(cs, module, station=station)
        return cs

    # 6) Drift: keep trying.
    cs.state = State.UPDATING
    fields = ["state", "updated_at"]
    if released:
        fields += ["attempts", "transient_failures", "reason_code"]
    cs.save(update_fields=fields)
    _set_convergence_rollup(module, Module.Convergence.UPDATING)
    return cs


def _give_up(cs, module, *, station=None):
    """updating -> failed at the transient cap; audits MODULE_FLASH_GAVE_UP once
    (callers only reach this from a non-failed row)."""
    cs.state = State.FAILED
    cs.reason_code = ModuleFirmwareConvergenceState.ReasonCode.TRANSIENT_LIMIT
    cs.save(update_fields=["state", "reason_code", "updated_at"])
    _set_convergence_rollup(module, Module.Convergence.FAILED)
    _audit(
        module,
        StationAuditLog.EventType.MODULE_FLASH_GAVE_UP,
        f"Modul {module.uid}: Update nach {cs.transient_failures} Fehlversuchen in Folge "
        f"abgebrochen (Ziel {cs.target_release.version}). "
        f"Letzte Meldung: {cs.last_error_message}",
        station=station,
    )


@transaction.atomic
def record_error(convergence, error_mode, error_message=""):
    """Apply a reported failure to a ConvergenceState per the quarantine rules:

    - rejected  -> immediate quarantine, attempts unchanged (retry never helps);
    - rolled_back -> attempts++, quarantine once attempts >= N;
    - transient -> no attempts++; transient_failures++ and, at
      DRAIN_TRANSIENT_LIMIT consecutive failures, ``failed``
      (reason transient_limit, audit MODULE_FLASH_GAVE_UP).

    Every mode ends the in-flight attempt: phase, phase_at and instructed_at are
    cleared. Stamps last_attempt_at/last_error_mode and maintains the rollup."""
    convergence.last_error_mode = error_mode
    convergence.last_error_message = error_message
    convergence.last_attempt_at = timezone.now()
    convergence.phase = ""
    convergence.phase_at = None
    convergence.instructed_at = None
    fields = [
        "last_error_mode",
        "last_error_message",
        "last_attempt_at",
        "phase",
        "phase_at",
        "instructed_at",
        "updated_at",
    ]
    give_up = False

    if error_mode == ModuleFirmwareConvergenceState.ErrorMode.REJECTED:
        convergence.state = State.QUARANTINED
        fields.append("state")
    elif error_mode == ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK:
        convergence.attempts += 1
        fields.append("attempts")
        if convergence.attempts >= QUARANTINE_ATTEMPT_LIMIT:
            convergence.state = State.QUARANTINED
            fields.append("state")
    else:
        # transient: no attempts change. N consecutive transients -> failed (the
        # verifying/commit resets of transient_failures keep this "in a row").
        convergence.transient_failures += 1
        fields.append("transient_failures")
        give_up = (
            convergence.state == State.UPDATING
            and convergence.transient_failures >= DRAIN_TRANSIENT_LIMIT
        )

    convergence.save(update_fields=fields)
    module = convergence.module

    if give_up:
        _give_up(convergence, module)
    elif convergence.state == State.QUARANTINED:
        was_quarantined = module.firmware_convergence == Module.Convergence.QUARANTINED
        _set_convergence_rollup(module, Module.Convergence.QUARANTINED)
        if not was_quarantined:
            message = (
                f"Module {module.uid} quarantined for target "
                f"{convergence.target_release.version} ({error_mode})."
            )
            if key_hint_applies(module, convergence.target_release, error_mode):
                message += f" {KEY_UNKNOWN_HINT}"
            _audit(module, StationAuditLog.EventType.MODULE_QUARANTINED, message)
    elif convergence.state in _STICKY_ROLLUP:
        _set_convergence_rollup(module, _STICKY_ROLLUP[convergence.state])
    else:
        _set_convergence_rollup(module, Module.Convergence.UPDATING)
    return convergence
