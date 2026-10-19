"""Sequence mutation service: locked, atomic, renumbering operations.

All public functions acquire a row-lock on the parent RolloutSequence before
mutating entries, so concurrent API / UI calls serialize through a single gate.
They also renumber remaining positions to maintain a 0-based, gap-free ordering
and stamp updated_by / updated_at on the parent in the same transaction.

These functions back both the API write path (apps/api/write_views.py) and the
UI add/remove views (apps/rollouts/views.py).
"""

from django.db import transaction
from django.db.models import Max

from .models import RolloutSequence, RolloutSequenceEntry

# PositiveSmallIntegerField upper bound
_POSITION_MAX = 32767


def _calc_offset(max_current: int, n: int) -> int:
    """Return the phase-1 offset for the two-phase position move.

    The offset is chosen so that ``max_current + offset`` stays within the
    PositiveSmallIntegerField bound (32767).  Raises ``ValueError`` if the
    sequence is too large to reorder safely (mirrors the UI guard in
    SequenceReorderView).
    """
    offset = max(max_current, n) + 1
    if max_current + offset > _POSITION_MAX:
        raise ValueError(
            f"Sequence too large to reorder safely: max position {max_current} + "
            f"offset {offset} = {max_current + offset} exceeds {_POSITION_MAX}."
        )
    return offset


def touch_sequence(sequence: RolloutSequence, by_user) -> None:
    """Stamp *sequence*.updated_by / updated_at without reordering entries.

    Acquires a SELECT FOR UPDATE lock on the parent row so it serialises
    with concurrent add/remove/move calls.  Used when a field other than
    ``position`` is mutated on an entry (e.g. a group-only PATCH).
    """
    with transaction.atomic():
        RolloutSequence.objects.select_for_update().filter(pk=sequence.pk).first()
        sequence.updated_by = by_user
        sequence.save(update_fields=["updated_by", "updated_at"])


def add_entry(
    sequence: RolloutSequence,
    group,
    by_user,
) -> RolloutSequenceEntry | None:
    """Add rollout *group* to *sequence* at the next available position.

    The ``position`` hint from the caller is intentionally not used:
    the service always appends at ``max(position) + 1`` so there are
    never position collisions on insert.  The caller must NOT pass a
    position — the service is the authority.

    Returns the new entry, or ``None`` if the group is already in the
    sequence (idempotent: does not raise).

    Acquires a SELECT FOR UPDATE lock on the parent RolloutSequence row,
    so two concurrent creates cannot both compute the same max(position)+1
    and race into an IntegrityError on uniq_position_per_sequence.
    """
    with transaction.atomic():
        RolloutSequence.objects.select_for_update().filter(pk=sequence.pk).first()
        if sequence.entries.filter(group=group).exists():
            return None
        max_pos = sequence.entries.aggregate(Max("position"))["position__max"]
        # Use explicit None-check: max_pos=0 is a valid position (the sequence has
        # exactly one entry at position 0) — `or -1` would incorrectly treat 0
        # as falsy and compute next_pos=0 again, causing a unique constraint error.
        next_pos = (max_pos if max_pos is not None else -1) + 1
        entry = RolloutSequenceEntry.objects.create(
            sequence=sequence,
            group=group,
            position=next_pos,
        )
        sequence.updated_by = by_user
        sequence.save(update_fields=["updated_by", "updated_at"])
    return entry


def remove_entry(entry: RolloutSequenceEntry, by_user) -> None:
    """Delete *entry* from its sequence and renumber remaining entries.

    After deletion all remaining positions are re-assigned from 0 so
    there are no gaps.  The parent sequence's updated_by / updated_at
    are stamped in the same atomic block.

    Acquires a SELECT FOR UPDATE lock on the parent RolloutSequence row.
    """
    sequence = entry.sequence
    with transaction.atomic():
        RolloutSequence.objects.select_for_update().filter(pk=sequence.pk).first()
        entry.delete()
        for idx, e in enumerate(sequence.entries.order_by("position")):
            if e.position != idx:
                e.position = idx
                e.save(update_fields=["position"])
        sequence.updated_by = by_user
        sequence.save(update_fields=["updated_by", "updated_at"])


def move_entry(entry: RolloutSequenceEntry, new_position: int, by_user) -> None:
    """Move *entry* to *new_position* using a two-phase update.

    The two-phase approach avoids unique-constraint collisions when
    swapping into an occupied slot:
      Phase 1: shift all entries to temporary positions (current + offset).
      Phase 2: assign final positions from the requested ordering.

    After the move the remaining entries are renumbered to be 0-based
    and gap-free.  The parent sequence's updated_by / updated_at are
    stamped in the same atomic block.

    Acquires SELECT FOR UPDATE locks on both the parent sequence row and
    all entry rows.
    """
    sequence = entry.sequence
    with transaction.atomic():
        RolloutSequence.objects.select_for_update().filter(pk=sequence.pk).first()
        existing = {e.pk: e for e in sequence.entries.select_for_update()}
        n = len(existing)
        max_current = max((e.position for e in existing.values()), default=0)
        offset = _calc_offset(max_current, n)

        # Phase 1: shift all to temporary positions to avoid constraint collisions
        for e in existing.values():
            e.position = e.position + offset
            e.save(update_fields=["position"])

        # Build the desired ordering: entry at new_position, rest maintain
        # their relative order.
        ordered_pks = [
            pk
            for pk in sorted(existing.keys(), key=lambda pk: existing[pk].position)
            if pk != entry.pk
        ]
        # Clamp new_position to valid range
        new_position = max(0, min(new_position, len(ordered_pks)))
        ordered_pks.insert(new_position, entry.pk)

        # Phase 2: assign final 0-based positions
        for idx, pk in enumerate(ordered_pks):
            e = existing[pk]
            e.position = idx
            e.save(update_fields=["position"])

        sequence.updated_by = by_user
        sequence.save(update_fields=["updated_by", "updated_at"])


def group_references(group) -> dict[str, int]:
    """Count what blocks deleting *group* (all four FKs are PROTECT).

    Separate ``count()`` queries instead of one annotated join: four
    multi-valued LEFT JOINs would multiply the row set.
    """
    return {
        "stations": group.stations.count(),
        "sequence_entries": group.rollout_entries.count(),
        "firmware_targets": group.firmware_targets.count(),
        "canary_targets": group.canary_targets.count(),
    }


GROUP_AUDIT_FIELDS = ("name", "slug", "channel", "description")


def group_snapshot(group) -> dict[str, str]:
    """Field values to diff against after an update (see :func:`audit_group`)."""
    return {f: str(getattr(group, f)) for f in GROUP_AUDIT_FIELDS}


def audit_group(group, action, *, actor, ip_address=None, suffix="", before=None) -> None:
    """CONFIG_CHANGED row for a rollout-group create/update/delete.

    Shared by the UI, the API and the Django admin so all three record the
    same shape. *before* is a :func:`group_snapshot` taken before an update;
    the row then carries an old/new diff. A create records the channel.
    """
    from apps.accounts.models import AccountAuditLog

    if before is not None:
        after = group_snapshot(group)
        changes = {
            f: {"old": before[f], "new": after[f]}
            for f in GROUP_AUDIT_FIELDS
            if before[f] != after[f]
        }
    elif action == "created":
        changes = {"channel": group.channel}
    else:
        changes = {}
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        actor=actor,
        ip_address=ip_address,
        message=f"RolloutGroup {group.slug} {action}{suffix}",
        details={"object": "rollout_group", "action": action, "changes": changes},
    )
