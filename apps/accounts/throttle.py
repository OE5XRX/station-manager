"""Rate-limit helpers for the Password-Reset endpoint.

Per-IP via Django cache (TTL-counter, simple but good enough at our scale).
Per-User via DB count of recent RESET tokens.
"""

from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from .models import AccountToken

_IP_KEY = "pwreset:ip:{ip}"
_IP_LIMIT = 10
_IP_WINDOW = 3600  # seconds

_USER_LIMIT = 3
_USER_WINDOW = timedelta(hours=1)


def _ip_rate_exceeded(ip):
    """Increment + check per-IP counter. Returns True if over limit.

    Implements a simple counter-with-TTL: first hit ``cache.add``s the
    key with TTL=_IP_WINDOW, subsequent hits ``cache.incr`` it, and
    the key expires after the window. Both operations are atomic on
    the supported backends (Redis in prod, LocMem in tests) — no race
    between concurrent workers seeing ``n == 0`` and double-resetting.

    NOT a perfect sliding window — at the very edge a user could get
    _IP_LIMIT requests across TWO windows in a short burst — but
    acceptable at Verein-internal scale.
    """
    key = _IP_KEY.format(ip=ip)
    # cache.add returns True iff the key did NOT exist (we just set it).
    # First request in the window lands here and is allowed.
    if cache.add(key, 1, timeout=_IP_WINDOW):
        return False
    # Key already exists; atomic increment.
    return cache.incr(key) > _IP_LIMIT


def _user_rate_exceeded(user):
    """Returns True if `user` has >= _USER_LIMIT RESET-tokens within _USER_WINDOW."""
    count = AccountToken.objects.filter(
        user=user,
        token_type=AccountToken.TokenType.RESET,
        created_at__gt=timezone.now() - _USER_WINDOW,
    ).count()
    return count >= _USER_LIMIT
