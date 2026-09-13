"""Module inventory ingestion. Pure ORM, synchronous — called from
apps.control.registry.apply_inventory inside its atomic transaction.

Assignment/swap and lifecycle derivation live in _apply_assignment /
_derive_lifecycle; ingest_module wires them in.
"""

import logging

from django.db import transaction

from apps.stations.models import StationAuditLog

from .models import Module, ModuleAssignmentHistory, ModuleType

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

    type_key = identity.get("type") or module_id
    try:
        module_type = ModuleType.objects.get(key=type_key)
    except ModuleType.DoesNotExist:
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.UPDATED,
            message=f"Ignored module with unregistered type '{type_key}' (uid={uid}).",
        )
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

    module, created = Module.objects.get_or_create(
        uid=uid,
        defaults={
            "module_type": module_type,
            "uid_source": uid_source,
            "last_reported_version": version,
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
        module.last_seen = now
        module.last_reported_version = version
        module.save(update_fields=["last_seen", "last_reported_version", "updated_at"])

    _apply_assignment(module, station, slot, now=now, user=user)
    _derive_lifecycle(module, now=now, station=station)
    return module


def _apply_assignment(module, station, slot, *, now, user=None):
    """Ensure exactly one open assignment for (module) at (station, slot).
    Closes any conflicting open rows and opens a new one on change.
    Returns True if the assignment changed."""
    # Serialize concurrent assignment mutations so the partial-unique
    # open-assignment constraints can't be tripped by a read-then-write race.
    # Two locks, always taken in the same order (station, then module) to avoid
    # deadlocks:
    #   - the Station row serializes two modules racing on the same (station,
    #     slot), i.e. concurrent applies for the same station;
    #   - the Module row serializes the same UID racing across stations.
    # No-op under SQLite in tests; real row locks under Postgres.
    station.__class__.objects.select_for_update().filter(pk=station.pk).exists()
    Module.objects.select_for_update().filter(pk=module.pk).exists()

    current = module.assignments.filter(to_ts__isnull=True).first()
    if current and current.station_id == station.id and current.slot == slot:
        return False  # unchanged — idempotent

    # Close this module's open assignment elsewhere.
    if current:
        current.to_ts = now
        current.save(update_fields=["to_ts"])

    # Close whatever other module currently occupies (station, slot).
    occupant = (
        ModuleAssignmentHistory.objects.filter(station=station, slot=slot, to_ts__isnull=True)
        .exclude(module=module)
        .first()
    )
    displaced_module = None
    if occupant:
        occupant.to_ts = now
        occupant.save(update_fields=["to_ts"])
        displaced_module = occupant.module

    ModuleAssignmentHistory.objects.create(
        module=module,
        station=station,
        slot=slot,
        from_ts=now,
        reason="auto-swap",
        created_by=user,
    )

    if displaced_module is not None:
        StationAuditLog.log(
            station=station,
            module=module,
            event_type=StationAuditLog.EventType.MODULE_SWAPPED,
            message=f"Module {module.uid} replaced a module in {station}/{slot}.",
        )
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
def reconcile_station(station, reported_slots, *, now):
    """Close open module assignments at ``station`` for slots absent from the
    latest full inventory snapshot, and re-derive the affected modules'
    lifecycle. A module physically pulled from a slot (its slot no longer
    reported) must lose its open assignment and fall back to ``ready`` — the
    per-module ingest path only runs for modules that ARE reported, so this
    closes the gap for the ones that vanished."""
    stale = (
        ModuleAssignmentHistory.objects.select_for_update()
        .filter(station=station, to_ts__isnull=True)
        .exclude(slot__in=reported_slots)
        .select_related("module")
    )
    for assignment in list(stale):
        assignment.to_ts = now
        assignment.save(update_fields=["to_ts"])
        module = assignment.module
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
    lifecycle transition."""
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
