from django.db import models
from django.utils.translation import gettext_lazy as _


class AudioGate(models.Model):
    """Per-station audio gating state (§5.5).

    Tracks PTT active/expires and the current TX route.  There is at most
    one row per station (OneToOne).  The sync ops in gate.py are the only
    mutation path.
    """

    station = models.OneToOneField(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="audio_gate",
    )
    ptt_active = models.BooleanField(_("PTT active"), default=False)
    ptt_slot = models.IntegerField(_("PTT slot"), null=True, blank=True)
    ptt_module = models.CharField(_("PTT module"), max_length=128, blank=True)
    ptt_expires_at = models.DateTimeField(_("PTT expires at"), null=True, blank=True)
    tx_slot = models.IntegerField(_("TX slot"), null=True, blank=True)
    tx_module = models.CharField(_("TX module"), max_length=128, blank=True)

    class Meta:
        verbose_name = _("audio gate")
        verbose_name_plural = _("audio gates")

    def __str__(self):
        return f"gate({self.station_id}) ptt={self.ptt_active} tx={self.tx_slot}/{self.tx_module}"


class AudioSubscription(models.Model):
    """One row per (browser connection, stream) demand entry (§6).

    ``channel_name`` is the Channels channel name for the browser consumer
    instance.  Together with ``stream_id`` it forms the logical key.
    ``subscriptions.py`` is the only mutation path — row counting there is
    worker-safe because it goes through the DB, mirroring TerminalSession.
    """

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="audio_subscriptions",
    )
    stream_id = models.CharField(_("stream id"), max_length=128)
    channel_name = models.CharField(_("channel name"), max_length=255)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("audio subscription")
        verbose_name_plural = _("audio subscriptions")
        constraints = [
            models.UniqueConstraint(
                fields=["station", "stream_id", "channel_name"],
                name="uniq_audio_sub_station_stream_channel",
            ),
        ]
        indexes = [
            models.Index(fields=["station", "stream_id"]),
            models.Index(fields=["station", "channel_name"]),
        ]

    def __str__(self):
        return f"sub({self.station_id}/{self.stream_id}/{self.channel_name})"
