"""Issue / consume / invalidate AccountToken — atomic, per-type."""

import hashlib
import secrets

from django.db import transaction
from django.utils import timezone

from .models import AccountToken


def issue_token(user, token_type, payload=None, ip=None):
    """Generate a fresh raw token, persist its hash, return the raw token.

    Caller MUST embed the raw token in a URL and send it to the user.
    After this call, the raw is gone from server memory unless caller
    holds the return value.
    """
    raw = secrets.token_urlsafe(32)
    AccountToken.objects.create(
        user=user,
        token_type=token_type,
        secret_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() + AccountToken.EXPIRY[token_type],
        payload=payload or {},
        ip_created=ip,
    )
    return raw


def consume_token(raw, expected_type):
    """Atomically validate + mark used. Returns the token row or None.

    None is returned if: token doesn't exist, wrong type, already used,
    or expired. Callers SHOULD NOT differentiate the failure cause to
    the end-user (timing-safe error).
    """
    secret_hash = hashlib.sha256(raw.encode()).hexdigest()
    with transaction.atomic():
        try:
            token = AccountToken.objects.select_for_update().get(
                secret_hash=secret_hash, token_type=expected_type
            )
        except AccountToken.DoesNotExist:
            return None
        if not token.is_active():
            return None
        token.used_at = timezone.now()
        token.save(update_fields=["used_at"])
        return token


def invalidate_pending_tokens(user, token_type):
    """Mark all unused tokens of `token_type` for `user` as used.

    Called when a fresh token of the same type is issued, so a user
    can only redeem the most recent one. Idempotent — already-used
    tokens are not modified.
    """
    AccountToken.objects.filter(user=user, token_type=token_type, used_at__isnull=True).update(
        used_at=timezone.now()
    )
