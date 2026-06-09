"""Delete TokenSession rows whose RefreshToken has been revoked or
expired for more than 30 days (Spec §4.5).

Idempotent: re-running is safe; rows that don't match the cutoff stay.

Three deletion criteria (any one suffices):
- refresh_token.revoked older than CUTOFF_DAYS (explicit revoke aged out)
- own revoked_at older than CUTOFF_DAYS (session marked revoked)
- issued_at older than (REFRESH_TOKEN_EXPIRE_SECONDS + CUTOFF_DAYS) —
  catches sessions that aged past the refresh-token lifetime but were
  never explicitly revoked (Copilot review #6).
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.sso.models import TokenSession

CUTOFF_DAYS = 30


class Command(BaseCommand):
    help = "Delete TokenSession rows older than CUTOFF_DAYS days (revoked or expired)."

    def handle(self, *args, **opts):
        now = timezone.now()
        cutoff = now - timedelta(days=CUTOFF_DAYS)
        lifetime_seconds = settings.OAUTH2_PROVIDER.get(
            "REFRESH_TOKEN_EXPIRE_SECONDS", 14 * 24 * 3600
        )
        # `issued_at` plus the refresh-lifetime is the natural end of
        # the session's usefulness; add the CUTOFF_DAYS forensic-grace
        # window on top.
        lifetime_grace_cutoff = now - timedelta(
            seconds=lifetime_seconds + CUTOFF_DAYS * 86400
        )
        qs = TokenSession.objects.filter(
            Q(refresh_token__revoked__lt=cutoff)
            | Q(revoked_at__lt=cutoff)
            | Q(issued_at__lt=lifetime_grace_cutoff),
        )
        n, _ = qs.delete()
        self.stdout.write(f"Pruned {n} TokenSession row(s).")
