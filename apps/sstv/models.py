"""SSTV pictures (gallery), transmissions and the running receive session.

Stored files (pictures, previews, TX audio) live in ``default_storage`` (S3 in
production) and are deleted together with their row (``signals``).
"""

import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.passive.models import _Singleton
from station_agent.sstv.modes import MODES

# Mode names are proper names ("Martin M1"), never translated.
MODE_CHOICES = [(m.key, m.name) for m in MODES.values()]
MODE_MAX_LENGTH = 16


class SstvSettings(_Singleton):
    retention_days = models.PositiveIntegerField(
        _("retention (days)"),
        default=90,
        validators=[MinValueValidator(1)],
        help_text=_("Pictures in the gallery are deleted after this many days."),
    )

    class Meta:
        verbose_name = _("SSTV settings")
        verbose_name_plural = _("SSTV settings")

    def __str__(self):
        return str(self._meta.verbose_name)

    @classmethod
    def load(cls):
        return cls.get()


class SstvImage(models.Model):
    """A received or sent picture in a station's gallery."""

    class Direction(models.TextChoices):
        RX = "rx", _("Received")
        TX = "tx", _("Sent")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    station = models.ForeignKey(
        "stations.Station",
        on_delete=models.CASCADE,
        related_name="sstv_images",
        verbose_name=_("station"),
    )
    direction = models.CharField(_("direction"), max_length=2, choices=Direction.choices)
    mode = models.CharField(_("mode"), max_length=MODE_MAX_LENGTH, choices=MODE_CHOICES)
    frequency_hz = models.BigIntegerField(_("frequency (Hz)"), null=True, blank=True)
    # RX: who started the receive session; TX: who sent the picture.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("user"),
    )
    # TX: snapshot of the sender's username (kept when the user is deleted). RX: blank.
    callsign = models.CharField(_("callsign"), max_length=150, blank=True)
    complete = models.BooleanField(_("complete"), default=True)
    image = models.ImageField(_("picture"), upload_to="sstv/")
    created_at = models.DateTimeField(_("created"), auto_now_add=True)
    # RX: the agent's picture id, dedupes resent frames. TX: blank.
    picture_id = models.CharField(_("picture ID"), max_length=64, blank=True)

    class Meta:
        verbose_name = _("SSTV picture")
        verbose_name_plural = _("SSTV pictures")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["station", "-created_at"], name="sstv_image_station_created")
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["station", "picture_id"],
                condition=~Q(picture_id=""),
                name="sstv_image_unique_picture_id",
            )
        ]

    def __str__(self):
        return f"{self.station} {self.direction} {self.mode} {self.created_at:%Y-%m-%d %H:%M}"


class SstvTransmission(models.Model):
    """A picture on its way out: draft (preview) → sending → done/aborted/error."""

    class State(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENDING = "sending", _("Sending")
        DONE = "done", _("Done")
        ABORTED = "aborted", _("Aborted")
        ERROR = "error", _("Error")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    station = models.ForeignKey(
        "stations.Station",
        on_delete=models.CASCADE,
        related_name="sstv_transmissions",
        verbose_name=_("station"),
    )
    slot = models.PositiveSmallIntegerField(_("slot"))
    module = models.CharField(_("module"), max_length=64)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("user"),
    )
    mode = models.CharField(_("mode"), max_length=MODE_MAX_LENGTH, choices=MODE_CHOICES)
    frequency_hz = models.BigIntegerField(_("frequency (Hz)"), null=True, blank=True)
    preview = models.ImageField(_("preview"), upload_to="sstv/preview/")
    audio = models.FileField(_("audio"), upload_to="sstv/tx/", null=True, blank=True)
    sha256 = models.CharField(_("SHA-256"), max_length=64, blank=True)
    size = models.PositiveBigIntegerField(_("size (bytes)"), default=0)
    duration_s = models.FloatField(_("duration (s)"), default=0.0)
    # Callsigns drawn onto the picture (empty = needs the "send without callsign" confirm).
    callsigns = models.JSONField(_("callsigns"), default=list, blank=True)
    state = models.CharField(
        _("state"), max_length=8, choices=State.choices, default=State.DRAFT, db_index=True
    )
    created_at = models.DateTimeField(_("created"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated"), auto_now=True)

    class Meta:
        verbose_name = _("SSTV transmission")
        verbose_name_plural = _("SSTV transmissions")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.station} {self.mode} {self.state}"


class ActiveSstvRx(models.Model):
    """The receive session running on a station (at most one)."""

    class State(models.TextChoices):
        STARTING = "starting", _("Starting")
        RUNNING = "running", _("Running")
        # Decoder stopped while an SSTV transmission is on the air; restarted after it.
        PAUSED = "paused", _("Paused")
        # Stop requested / agent gone: kept until the agent reports the decoder stopped,
        # so the final picture (sent just before that) is still attributed to the user.
        STOPPING = "stopping", _("Stopping")

    station = models.OneToOneField(
        "stations.Station",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="active_sstv_rx",
        verbose_name=_("station"),
    )
    slot = models.PositiveSmallIntegerField(_("slot"))
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("user"),
    )
    state = models.CharField(
        _("state"), max_length=8, choices=State.choices, default=State.STARTING
    )
    started_at = models.DateTimeField(_("started"), auto_now_add=True)
    # request_id of the decoder_start this session is waiting for: ties the agent's result
    # to this (re)start, never to an earlier one.
    start_request = models.CharField(_("start request"), max_length=64, blank=True)

    class Meta:
        verbose_name = _("active SSTV receive session")
        verbose_name_plural = _("active SSTV receive sessions")

    def __str__(self):
        return f"{self.station} slot {self.slot} {self.state}"
