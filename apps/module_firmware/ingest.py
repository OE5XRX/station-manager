"""Module inventory ingestion. Pure ORM, synchronous — called from
apps.control.registry.apply_inventory inside its atomic transaction.

Assignment/swap and lifecycle derivation live in _apply_assignment /
_derive_lifecycle; ingest_module wires them in.
"""

import logging

from django.db import transaction

from apps.stations.models import StationAuditLog

from .models import Module, ModuleAssignmentHistory, ModuleType
from .reconciler import reconcile_module

logger = logging.getLogger(__name__)


@transaction.atomic
def ingest_module(station, slot, module_id, identity, *, now, user=None):
    """Upsert a Module from a self-reported identity dict. Returns the Module
    or None (no UID => legacy path; unknown type => rejected).

    Wrapped in ``transaction.atomic`` so the row locks taken during assignment
    are always inside a transaction — safe both under ``apply_inventory``'s
    outer atomic (nested savepoint) and when called directly (e.g. tests) on a
    backend that enforces the ``select_for_update`` transaction contract."""
    identity = identity or {}
    uid = identity.get("uid")
    if not uid:
        return None  # legacy firmware without UID: StationModule-only display

    # Slot arrives as an int from the broker wire shape, but slot is stored as a
    # CharField. Normalize to str so the idempotency comparison in
    # _apply_assignment (current.slot == slot) doesn't fail "1" != 1 on every
    # heartbeat and churn the assignment history.
    slot = "" if slot is None else str(slot)

    # Resolve the registry type by the WIRE module id (the ``module list`` id,
    # e.g. "fm"/"gps"), which is what ModuleType.key is keyed on — NOT the
    # descriptor's identity.type ("fm_transceiver"/"gnss"), which is a longer
    # model-level string that would never match the registry and would reject
    # every real frame. Fall back to identity.type only if module_id is absent.
    type_key = module_id or identity.get("type")
    try:
        module_type = ModuleType.objects.get(key=type_key)
    except ModuleType.DoesNotExist:
        # Log only — do NOT write an audit row: rejected UIDs are never
        # persisted, so a station running unregistered-type firmware would
        # append one audit row per heartbeat (unbounded growth).
        logger.warning("ingest: unknown module type %r (uid=%s)", type_key, uid)
        return None

    # uid_source is a TextChoices field but Django does not enforce choices on
    # save(); guard against a malformed self-reported value creating an invalid
    # third source. Fall back to stm32_uid.
    reported_source = identity.get("uid_source") or Module.UidSource.STM32_UID
    uid_source = (
        reported_source
        if reported_source in Module.UidSource.values
        else Module.UidSource.STM32_UID
    )
    version = identity.get("version", "")
    variant = identity.get("variant", "") or ""

    module, created = Module.objects.get_or_create(
        uid=uid,
        defaults={
            "module_type": module_type,
            "uid_source": uid_source,
            "last_reported_version": version,
            "variant": variant,
            "first_seen": now,
            "last_seen": now,
        },
    )
    if created:
        StationAuditLog.log(
            station=station,
            module=module,
            event_type=StationAuditLog.EventType.MODULE_DISCOVERED,
            message=f"Module {uid} ({module_type.key}) discovered in {station}/{slot}.",
        )
    else:
        # A UID is a physical module's stable identity; its type must not change.
        # A report claiming a different registered type is a UID collision or a
        # firmware misreport — reject it rather than silently re-typing / linking
        # the wrong module, and audit the anomaly.
        if module.module_type_id != module_type.id:
            # Audit once per anomaly, not per heartbeat: a persistent misreport
            # would otherwise grow the audit table without bound.
            mismatch_msg = (
                f"Type mismatch for uid {uid}: reported '{module_type.key}', "
                f"tracked as '{module.module_type.key}'. Ignored."
            )
            already_audited = StationAuditLog.objects.filter(
                module=module,
                event_type=StationAuditLog.EventType.UPDATED,
                message=mismatch_msg,
            ).exists()
            if not already_audited:
                StationAuditLog.log(
                    station=station,
                    module=module,
                    event_type=StationAuditLog.EventType.UPDATED,
                    message=mismatch_msg,
                )
            logger.warning(
                "ingest: type mismatch uid=%s reported=%s tracked=%s",
                uid,
                module_type.key,
                module.module_type.key,
            )
            return None
        # variant is a fixed HW property: fill a blank once, but a change
        # between two non-blank variants is an anomaly (audit once, ignore).
        reported_variant = identity.get("variant", "") or ""
        if reported_variant:
            if not module.variant:
                module.variant = reported_variant
                module.save(update_fields=["variant", "updated_at"])
            elif module.variant != reported_variant:
                variant_msg = (
                    f"Variant mismatch for uid {uid}: reported "
                    f"'{reported_variant}', tracked as '{module.variant}'. Ignored."
                )
                already = StationAuditLog.objects.filter(
                    module=module,
                    event_type=StationAuditLog.EventType.UPDATED,
                    message=variant_msg,
                ).exists()
                if not already:
                    StationAuditLog.log(
                        station=station,
                        module=module,
                        event_type=StationAuditLog.EventType.UPDATED,
                        message=variant_msg,
                    )
                logger.warning(
                    "ingest: variant mismatch uid=%s reported=%s tracked=%s",
                    uid,
                    reported_variant,
                    module.variant,
                )
        old_version = module.last_reported_version
        module.last_seen = now
        module.last_reported_version = version
        module.save(update_fields=["last_seen", "last_reported_version", "updated_at"])
        # Record firmware version transitions so the detail view has real history
        # (skip the no-op and the very first observed version).
        if version and old_version and version != old_version:
            StationAuditLog.log(
                station=station,
                module=module,
                event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                message=f"Module {uid} reported version {old_version} → {version}.",
                changes={"last_reported_version": {"old": old_version, "new": version}},
            )

    _apply_assignment(module, station, slot, now=now, user=user)
    _derive_lifecycle(module, now=now, station=station)
    try:
        reconcile_module(module)
    except Exception:
        logger.warning("ingest: reconcile_module failed for uid=%s", uid, exc_info=True)
    return module


def _apply_assignment(module, station, slot, *, now, user=None):
    """Ensure exactly one open assignment for (module) at (station, slot).
    Closes any conflicting open rows and opens a new one on change.
    Returns True if the assignment changed."""
    # Serialize concurrent assignment mutations so the partial-unique
    # open-assignment constraints can't be tripped by a read-then-write race and
    # so no lock cycle can form. Deterministic lock order:
    #   1. the Station row (serializes concurrent applies for the same station);
    #   2. every involved Module row, in ascending pk order, acquired BEFORE any
    #      assignment row is mutated — so two transactions touching the same pair
    #      of modules always take the locks in the same order (no deadlock).
    # No-op under SQLite in tests; real row locks under Postgres.
    station.__class__.objects.select_for_update().filter(pk=station.pk).exists()

    # Pre-lock read, only to discover which extra module row to lock (the
    # current occupant of this slot). ``module`` itself is always locked.
    pre_occupant = (
        ModuleAssignmentHistory.objects.filter(station=station, slot=slot, to_ts__isnull=True)
        .exclude(module=module)
        .first()
    )
    module_pks = {module.pk}
    if pre_occupant:
        module_pks.add(pre_occupant.module_id)
    # Lock all involved module rows up front, ascending pk order (no lock cycle).
    list(Module.objects.select_for_update().filter(pk__in=sorted(module_pks)))
    # Refresh under the locks: a concurrent set_lifecycle may have committed a
    # sticky state, and a concurrent apply for the same UID may have opened an
    # assignment, after the pre-lock reads. Re-read authoritative state now.
    module.refresh_from_db()
    current = module.assignments.filter(to_ts__isnull=True).first()
    if current and current.station_id == station.id and current.slot == slot:
        return False  # unchanged — idempotent (re-checked under the lock)

    occupant = (
        ModuleAssignmentHistory.objects.filter(station=station, slot=slot, to_ts__isnull=True)
        .exclude(module=module)
        .first()
    )

    # Close this module's open assignment elsewhere (a relocation).
    relocated = False
    if current:
        current.to_ts = now
        current.save(update_fields=["to_ts"])
        relocated = True

    displaced_module = None
    if occupant:
        occupant.to_ts = now
        occupant.save(update_fields=["to_ts"])
        displaced_module = occupant.module
        displaced_module.refresh_from_db()

    ModuleAssignmentHistory.objects.create(
        module=module,
        station=station,
        slot=slot,
        from_ts=now,
        reason="auto-swap",
        created_by=user,
    )

    # A slot-content change — the module displaced an occupant OR relocated from
    # a previous slot — is a swap per the spec: emit MODULE_SWAPPED.
    if displaced_module is not None or relocated:
        if displaced_module is not None:
            swap_msg = f"Module {module.uid} replaced a module in {station}/{slot}."
        else:
            swap_msg = f"Module {module.uid} relocated to {station}/{slot}."
        StationAuditLog.log(
            station=station,
            module=module,
            event_type=StationAuditLog.EventType.MODULE_SWAPPED,
            message=swap_msg,
        )
    if displaced_module is not None:
        # The displaced module lost its slot — recompute its lifecycle so it
        # falls back from deployed to ready (unless operator-sticky).
        _derive_lifecycle(displaced_module, now=now, station=station)
    StationAuditLog.log(
        station=station,
        module=module,
        event_type=StationAuditLog.EventType.MODULE_ASSIGNMENT_CHANGED,
        message=f"Module {module.uid} assigned to {station}/{slot}.",
    )
    return True


@transaction.atomic
def reconcile_station(station, tracked_slots, *, now, reported_uid_by_slot=None):
    """Close open module assignments at ``station`` for slots that no longer
    hold their tracked module, and re-derive the affected modules' lifecycle. A
    module physically pulled from a slot (its slot no longer reporting that
    module) must lose its open assignment and fall back to ``ready`` — the
    per-module ingest path only runs for modules that ARE linked, so this closes
    the gap for the ones that vanished.

    ``tracked_slots`` = slots that reported a successfully linked Module.
    ``reported_uid_by_slot`` preserves an assignment when the SAME uid is still
    reported in its slot but the entry was rejected (e.g. type mismatch), so a
    spurious report can't tear down a valid module's assignment."""
    reported_uid_by_slot = reported_uid_by_slot or {}
    # Lock the affected Module rows FIRST, in ascending pk order — the SAME
    # order _apply_assignment uses (module -> assignment). A pre-read (no lock)
    # discovers which modules are involved; locking them before the assignment
    # rows keeps the global lock order module→assignment everywhere, so a
    # reconcile can't deadlock against a concurrent cross-station move.
    stale_module_ids = sorted(
        set(
            ModuleAssignmentHistory.objects.filter(station=station, to_ts__isnull=True)
            .exclude(slot__in=tracked_slots)
            .values_list("module_id", flat=True)
        )
    )
    if not stale_module_ids:
        return
    list(Module.objects.select_for_update().filter(pk__in=stale_module_ids))

    stale = (
        ModuleAssignmentHistory.objects.select_for_update()
        .filter(station=station, to_ts__isnull=True)
        .exclude(slot__in=tracked_slots)
        .select_related("module")
    )
    for assignment in list(stale):
        module = assignment.module
        # Same physical module still reported in this slot (rejected entry) — keep.
        if reported_uid_by_slot.get(assignment.slot) == module.uid:
            continue
        assignment.to_ts = now
        assignment.save(update_fields=["to_ts"])
        StationAuditLog.log(
            station=station,
            module=module,
            event_type=StationAuditLog.EventType.MODULE_ASSIGNMENT_CHANGED,
            message=f"Module {module.uid} no longer reported in {station}/{assignment.slot}.",
        )
        _derive_lifecycle(module, now=now, station=station)


def _derive_lifecycle(module, *, now, station=None):
    """Derive lifecycle from assignment state. Never overrides sticky states.

    ``station`` is the station whose ingest triggered this derivation; passing
    it makes the audit entry dual-subject so station-centric views also show the
    lifecycle transition.

    Locks + refreshes the module row first (all callers run inside an atomic):
    a concurrent set_lifecycle may have committed a sticky state after this
    ``module`` object was read, and without the reload the sticky check below
    could see stale state and overwrite the operator's status."""
    Module.objects.select_for_update().filter(pk=module.pk).exists()
    module.refresh_from_db()
    if module.lifecycle_status in Module.STICKY_LIFECYCLE:
        return
    has_open = module.assignments.filter(to_ts__isnull=True).exists()
    target = Module.Lifecycle.DEPLOYED if has_open else Module.Lifecycle.READY
    if module.lifecycle_status != target:
        old = module.lifecycle_status
        module.lifecycle_status = target
        module.save(update_fields=["lifecycle_status", "updated_at"])
        StationAuditLog.log(
            station=station,
            module=module,
            event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED,
            message=f"Lifecycle {old} → {target} for module {module.uid}.",
            changes={"lifecycle_status": {"old": old, "new": target}},
        )
