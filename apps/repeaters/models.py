from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class RepeaterQuerySet(models.QuerySet):
    def visible(self):
        """Active rows, minus ÖVSV rows replaced by an active manual entry."""
        return self.filter(active=True).exclude(
            pk__in=Repeater.objects.filter(active=True, replaces__isnull=False).values(
                "replaces_id"
            )
        )


class Repeater(models.Model):
    class Source(models.TextChoices):
        OEVSV = "oevsv", _("ÖVSV")
        MANUAL = "manual", _("Manual")

    callsign = models.CharField(_("callsign"), max_length=32)
    site_name = models.CharField(_("site name"), max_length=128, blank=True)
    locator = models.CharField(_("locator"), max_length=10, blank=True)
    # Station perspective: we RX on output_frequency and TX on input_frequency.
    output_frequency = models.DecimalField(
        _("output frequency (MHz)"), max_digits=9, decimal_places=5
    )
    input_frequency = models.DecimalField(
        _("input frequency (MHz)"), max_digits=9, decimal_places=5
    )
    input_tone = models.CharField(_("input tone"), max_length=8, default="none")
    output_tone = models.CharField(_("output tone"), max_length=8, default="none")
    bandwidth = models.CharField(
        _("bandwidth"),
        max_length=4,
        blank=True,
        choices=[("12.5", "12.5"), ("25", "25"), ("", "—")],
    )
    latitude = models.DecimalField(
        _("latitude"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        _("longitude"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    source = models.CharField(_("source"), max_length=8, choices=Source.choices)
    external_id = models.CharField(
        _("external id"), max_length=64, null=True, blank=True, unique=True
    )
    active = models.BooleanField(_("active"), default=True)
    replaces = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="replaced_by",
        verbose_name=_("replaces"),
    )
    comment = models.TextField(_("comment"), blank=True)
    last_synced_at = models.DateTimeField(_("last synced at"), null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = RepeaterQuerySet.as_manager()

    class Meta:
        verbose_name = _("repeater")
        verbose_name_plural = _("repeaters")
        ordering = ["callsign", "output_frequency"]

    def __str__(self):
        return f"{self.callsign} {self.output_frequency}"

    @property
    def is_simplex(self) -> bool:
        return self.input_frequency == self.output_frequency


class ScanList(models.Model):
    """A user's own, ordered scan list; goes with the user to every station."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="scan_lists",
        verbose_name=_("owner"),
    )
    name = models.CharField(_("name"), max_length=64)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("scan list")
        verbose_name_plural = _("scan lists")
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class ScanListEntry(models.Model):
    """Either a reference to a repeater or a free (simplex) frequency."""

    scan_list = models.ForeignKey(ScanList, on_delete=models.CASCADE, related_name="entries")
    position = models.PositiveIntegerField(_("position"), default=0)
    repeater = models.ForeignKey(
        Repeater,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="scan_entries",
        verbose_name=_("repeater"),
    )
    frequency = models.DecimalField(
        _("frequency (MHz)"), max_digits=9, decimal_places=5, null=True, blank=True
    )
    tone = models.CharField(_("tone"), max_length=8, default="none")
    label = models.CharField(_("label"), max_length=64, blank=True)

    class Meta:
        verbose_name = _("scan list entry")
        verbose_name_plural = _("scan list entries")
        ordering = ["position", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(repeater__isnull=False, frequency__isnull=True)
                    | models.Q(repeater__isnull=True, frequency__isnull=False)
                ),
                name="scanlistentry_repeater_xor_frequency",
            )
        ]

    def clean(self):
        super().clean()
        if (self.repeater_id is None) == (self.frequency is None):
            raise ValidationError(_("Set either a repeater or a frequency, not both or neither."))

    def __str__(self):
        return self.label or (str(self.repeater) if self.repeater_id else f"{self.frequency}")
