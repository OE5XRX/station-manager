from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class RolloutSequence(models.Model):
    """Singleton-in-practice: system-wide ordered tag list for manual phased
    rollouts. Created once via data migration, edited via the Rollout
    Sequence page.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_rollout_sequences",
    )

    class Meta:
        verbose_name = _("rollout sequence")
        verbose_name_plural = _("rollout sequences")

    def __str__(self):
        return f"RolloutSequence #{self.pk}"


class RolloutSequenceEntry(models.Model):
    """One tag at one position inside a RolloutSequence."""

    sequence = models.ForeignKey(
        RolloutSequence,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    tag = models.ForeignKey(
        "stations.StationTag",
        on_delete=models.CASCADE,
        related_name="rollout_entries",
    )
    position = models.PositiveSmallIntegerField(_("position"))

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["sequence", "tag"], name="uniq_tag_per_sequence"),
            models.UniqueConstraint(
                fields=["sequence", "position"], name="uniq_position_per_sequence"
            ),
        ]

    def __str__(self):
        return f"{self.position}: {self.tag}"


def current_sequence() -> RolloutSequence:
    """Return the singleton RolloutSequence.

    The row is seeded by migration 0002_seed_singleton; this helper
    returns whichever RolloutSequence exists, or creates one (without
    forcing a specific pk) if the table is somehow empty.

    We deliberately avoid a hard-coded pk — on Postgres, explicit-pk
    inserts don't advance the underlying sequence, which would cause
    later Django-admin-style creates to collide on pk=1.
    """
    seq = RolloutSequence.objects.order_by("pk").first()
    if seq is None:
        seq = RolloutSequence.objects.create()
    return seq
