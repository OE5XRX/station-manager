"""Deliver a single Web-Push message via pywebpush.

Kept import-light and side-effect-scoped so the monitoring dispatch can
iterate subscriptions and isolate per-device failures. Expired endpoints
(404/410) are pruned on the spot; transient errors bump a failure counter.
"""

import json
import logging

from django.conf import settings
from django.utils import timezone
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)


def send_web_push(subscription, payload):
    """Send ``payload`` (a JSON-serialisable dict) to one subscription.

    Returns True on success. On 404/410 the subscription is deleted and
    False returned. On any other error failure_count is incremented and
    False returned.
    """
    try:
        webpush(
            subscription_info=subscription.subscription_info,
            data=json.dumps(payload),
            vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.WEBPUSH_VAPID_ADMIN_EMAIL},
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            logger.info("Pruning expired push subscription %s (%s).", subscription.pk, status)
            subscription.delete()
        else:
            logger.warning("Web-push failed for subscription %s: %s", subscription.pk, exc)
            subscription.failure_count += 1
            subscription.save(update_fields=["failure_count"])
        return False
    except Exception:
        logger.exception("Unexpected web-push error for subscription %s.", subscription.pk)
        subscription.failure_count += 1
        subscription.save(update_fields=["failure_count"])
        return False

    subscription.last_success_at = timezone.now()
    subscription.failure_count = 0
    subscription.save(update_fields=["last_success_at", "failure_count"])
    return True
