"""Web-Push subscription storage.

One row per browser push endpoint. The endpoint URL (issued by the
platform push service — Apple/Mozilla/Google) is the natural unique key:
re-subscribing the same browser yields the same endpoint, so we upsert
rather than duplicate. VAPID handling and delivery live in
``apps/webpush/dispatch.py``; this module is storage only.
"""

from django.conf import settings
from django.db import models


class PushSubscription(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    label = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} · {self.label or self.endpoint[:40]}"

    @property
    def subscription_info(self):
        """Return the dict shape pywebpush expects."""
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }
