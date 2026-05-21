"""Cascade user/grant lifecycle events onto OAuth2 token state.

Two pre_save/post_save handler pairs:

User.is_active False-edge → revoke ALL access+refresh tokens for that user.
  Without this, a deactivated user keeps working in any RP until their
  current access/refresh tokens expire naturally (up to refresh-lifetime
  of 14 days per spec). The cascade closes that window immediately.

AppGrant.revoked_at None→datetime edge → revoke tokens scoped to that
  (user, application) pair only.  Other apps the user still has grants
  for keep working.

Implementation note: we set `AccessToken.expires` to now-1s and
`RefreshToken.revoked` to now, rather than deleting rows. Audit-trail
preservation matches the AppGrant model's soft-delete pattern.

pre_save stashes the OLD field value on the instance so post_save can
detect the transition (Django doesn't expose pre-save state otherwise).
"""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User.is_active False-edge
# ---------------------------------------------------------------------------


@receiver(pre_save, sender=get_user_model())
def _stash_old_is_active(sender, instance, **kwargs):
    """Stash pre-save value of is_active so post_save can compare."""
    if instance.pk is None:
        instance._old_is_active = True  # creation defaults to active
        return
    try:
        old = sender.objects.only("is_active").get(pk=instance.pk)
        instance._old_is_active = old.is_active
    except sender.DoesNotExist:
        instance._old_is_active = True


@receiver(post_save, sender=get_user_model())
def _revoke_tokens_on_user_deactivation(sender, instance, created, **kwargs):
    if created:
        return
    old = getattr(instance, "_old_is_active", True)
    if old and not instance.is_active:
        from oauth2_provider.models import AccessToken, RefreshToken

        past = timezone.now() - timedelta(seconds=1)
        n_at = AccessToken.objects.filter(user=instance, expires__gt=timezone.now()).update(
            expires=past
        )
        n_rt = RefreshToken.objects.filter(user=instance, revoked__isnull=True).update(
            revoked=timezone.now()
        )
        logger.info(
            "User %s deactivated → revoked %d access + %d refresh tokens",
            instance.username,
            n_at,
            n_rt,
        )

        # Audit log is best-effort: a transient DB error on the audit
        # write must not undo the deactivation that already committed.
        try:
            from .models import SsoAuditLog

            SsoAuditLog.log(
                event_type=SsoAuditLog.EventType.TOKEN_REVOKED,
                target_user=instance,
                message=(
                    f"User deactivated; {n_at} access tokens + {n_rt} refresh tokens revoked."
                ),
            )
        except Exception:
            logger.exception("Audit log write failed during user deactivation cascade")


# ---------------------------------------------------------------------------
# AppGrant revoked_at None→datetime edge
# ---------------------------------------------------------------------------


def _revoke_tokens_for_user_and_app(user, application):
    """Helper called from the AppGrant post_save handler below."""
    from oauth2_provider.models import AccessToken, RefreshToken

    past = timezone.now() - timedelta(seconds=1)
    n_at = AccessToken.objects.filter(
        user=user, application=application, expires__gt=timezone.now()
    ).update(expires=past)
    n_rt = RefreshToken.objects.filter(
        user=user, application=application, revoked__isnull=True
    ).update(revoked=timezone.now())
    logger.info(
        "AppGrant revoked user=%s app=%s → %d access + %d refresh revoked",
        user.username,
        application.client_id,
        n_at,
        n_rt,
    )

    try:
        from .models import SsoAuditLog

        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.TOKEN_REVOKED,
            target_user=user,
            application=application,
            message=(f"AppGrant revoked; {n_at} access + {n_rt} refresh tokens revoked."),
        )
    except Exception:
        logger.exception("Audit log write failed during grant revoke cascade")


@receiver(pre_save, sender="sso.AppGrant")
def _stash_old_revoked_at(sender, instance, **kwargs):
    if instance.pk is None:
        instance._old_revoked_at = None
        return
    try:
        old = sender.objects.only("revoked_at").get(pk=instance.pk)
        instance._old_revoked_at = old.revoked_at
    except sender.DoesNotExist:
        instance._old_revoked_at = None


@receiver(post_save, sender="sso.AppGrant")
def _revoke_tokens_on_grant_revoke(sender, instance, created, **kwargs):
    if created:
        return
    old = getattr(instance, "_old_revoked_at", None)
    if old is None and instance.revoked_at is not None:
        _revoke_tokens_for_user_and_app(instance.user, instance.application)
