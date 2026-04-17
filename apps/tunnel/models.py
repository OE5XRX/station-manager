from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class TerminalSession(models.Model):
    """Tracks browser-to-station terminal sessions."""

    class Status(models.TextChoices):
        CONNECTING = "connecting", _("Connecting")
        ACTIVE = "active", _("Active")
        CLOSED = "closed", _("Closed")
        ERROR = "error", _("Error")

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="terminal_sessions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="terminal_sessions",
    )
    status = models.CharField(
        _("status"),
        max_length=12,
        choices=Status.choices,
        default=Status.CONNECTING,
    )
    started_at = models.DateTimeField(_("started at"), auto_now_add=True)
    ended_at = models.DateTimeField(_("ended at"), null=True, blank=True)
    close_reason = models.CharField(_("close reason"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("terminal session")
        verbose_name_plural = _("terminal sessions")
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.station} - {self.user} - {self.get_status_display()}"
