"""Delete TokenSession rows whose RefreshToken has been revoked or
expired for more than 30 days (Spec §4.5).

Idempotent: re-running is safe; rows that don't match the cutoff stay.
The criteria live in ``TokenSessionQuerySet.stale`` (shared with the
retention job ``enforce_retention``).
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.sso.models import STALE_CUTOFF_DAYS, TokenSession


class Command(BaseCommand):
    help = f"Delete TokenSession rows older than {STALE_CUTOFF_DAYS} days (revoked or expired)."

    def handle(self, *args, **opts):
        n, _ = TokenSession.objects.stale(timezone.now()).delete()
        self.stdout.write(f"Pruned {n} TokenSession row(s).")
