"""Delete TokenSession rows whose RefreshToken has been revoked or
expired for more than 30 days (Spec §4.5).

Idempotent: re-running is safe; rows that don't match the cutoff stay.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.sso.models import TokenSession


CUTOFF_DAYS = 30


class Command(BaseCommand):
    help = "Delete TokenSession rows older than CUTOFF_DAYS days (revoked or expired)."

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=CUTOFF_DAYS)
        qs = TokenSession.objects.filter(
            Q(refresh_token__revoked__lt=cutoff)
            | Q(revoked_at__lt=cutoff),
        )
        n = qs.count()
        qs.delete()
        self.stdout.write(f"Pruned {n} TokenSession row(s).")
