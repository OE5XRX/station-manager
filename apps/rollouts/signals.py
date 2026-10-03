"""Signal handlers for rollout sequence integrity.

post_delete on RolloutSequenceEntry: whenever an entry is removed (whether via
the API's own remove_entry service, OR via a CASCADE from a StationTag delete),
renumber remaining entries to be 0-based and gap-free, and bump the parent
RolloutSequence.updated_at.

The renumber is idempotent: running it twice (e.g. when remove_entry also
renumbers after deleting the entry) produces the same result — the positions are
already gap-free on the second pass, so all `e.position == idx` checks pass
without writing.

updated_by is intentionally left unchanged here: the signal has no actor context
(cascade deletes arrive without a request user).  The service-layer callers
(remove_entry, touch_sequence) already set updated_by when they have an actor.
"""

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import RolloutSequence, RolloutSequenceEntry


@receiver(post_delete, sender=RolloutSequenceEntry)
def _on_sequence_entry_delete(sender, instance, **kwargs):
    """Renumber the parent sequence after any entry removal.

    Fires for both API-driven deletes (remove_entry) and CASCADE-driven
    deletes (StationTag.delete() → CASCADE → RolloutSequenceEntry.delete()).

    The renumber uses the already-established transaction (the caller's atomic
    block, or the implicit atomic wrapping a plain .delete() call).

    R5a — SELECT FOR UPDATE on parent:
      The signal acquires a lock on the parent RolloutSequence row before
      reading and renumbering entries, so it serializes with add_entry /
      move_entry (which also hold the parent lock).  This prevents a
      concurrent add/move from interleaving with the signal-driven renumber
      and producing gaps or position collisions.

      Guard: if the parent no longer exists (e.g. a cascade from a parent
      RolloutSequence delete), skip safely — no renumber needed.
    """
    # Guard against a detached instance (sequence_id == None) or a parent
    # that is already being deleted in the same cascade.
    sequence_id = getattr(instance, "sequence_id", None)
    if not sequence_id:
        return

    # Lock the parent sequence row so the renumber serializes with concurrent
    # add_entry / move_entry calls.  If the parent no longer exists (cascade),
    # first() returns None and we skip safely.
    with transaction.atomic():
        sequence = RolloutSequence.objects.select_for_update().filter(pk=sequence_id).first()
        if sequence is None:
            return

        # Renumber remaining entries gap-free.  Uses update_fields so auto_now
        # fields on the entry itself are not touched.
        for idx, e in enumerate(sequence.entries.order_by("position")):
            if e.position != idx:
                e.position = idx
                e.save(update_fields=["position"])

        # Stamp updated_at on the parent (updated_by left as caller's responsibility).
        sequence.save(update_fields=["updated_at"])
