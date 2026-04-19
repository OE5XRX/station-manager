from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class RolloutSequence(models.Model):
    """System-wide ordered tag list for manual phased rollouts.

    Enforced singleton: the ``singleton_key`` field has a unique index
    and a fixed default, so the DB rejects any attempt to insert a
    second row (whether from a race inside ``current_sequence()`` or
    from an admin creating one by hand).
    """

    SINGLETON_KEY = "current"

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_rollout_sequences",
    )
    singleton_key = models.CharField(
        max_length=16,
        default=SINGLETON_KEY,
        editable=False,
        unique=True,
        help_text=_("Fixed value; the unique index makes this table a singleton."),
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
    """Return the singleton RolloutSequence, creating it if missing.

    Two requests racing through this function on an empty table used
    to be able to create two rows; the ``singleton_key`` unique index
    plus ``get_or_create`` now serializes them at the DB level — the
    loser hits IntegrityError inside ``get_or_create`` which re-reads
    the row the winner inserted.
    """
    seq, _ = RolloutSequence.objects.get_or_create(singleton_key=RolloutSequence.SINGLETON_KEY)
    return seq
