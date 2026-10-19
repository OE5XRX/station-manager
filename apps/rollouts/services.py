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


def delete_group(group, *, by_user, ip_address=None, audit_suffix: str = "") -> None:
    """Delete a RolloutGroup with the side effects both UI and API rely on.

    In one transaction:
      - one ``ROLLOUT_GROUP_CHANGED`` StationAuditLog row per member station
        (members fall back to "no group" via SET_NULL, which leaves no trace
        on its own); ``audit_suffix`` is appended to the message (the API
        passes its token-origin suffix),
      - every sequence entry of the group is removed through
        :func:`remove_entry`, so the sequence is renumbered and stamped with
        *by_user* (the CASCADE's post_delete renumber alone has no actor),
      - the group itself is deleted.
    """
    from apps.stations.models import StationAuditLog

    with transaction.atomic():
        for station in group.stations.all():
            StationAuditLog.log(
                station=station,
                event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED,
                message=(
                    f"Rollout group: {group.name} → none by {by_user} (group deleted)"
                    f"{audit_suffix}"
                ),
                changes={"rollout_group": {"old": group.name, "new": "none"}},
                user=by_user,
                ip_address=ip_address,
            )
        for entry in group.rollout_entries.select_related("sequence"):
            remove_entry(entry, by_user=by_user)
        group.delete()
