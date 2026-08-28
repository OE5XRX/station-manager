"""Notification dispatch for alerts (email, Telegram, and Web-Push)."""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from apps.webpush.dispatch import send_web_push

logger = logging.getLogger(__name__)


def send_alert_notifications(alert):
    """Dispatch alert via configured channels."""
    if getattr(settings, "ALERT_EMAIL_ENABLED", False):
        _send_email_notification(alert)
    if getattr(settings, "ALERT_TELEGRAM_ENABLED", False):
        _send_telegram_notification(alert)
    if getattr(settings, "ALERT_WEBPUSH_ENABLED", False):
        _send_webpush_notification(alert)


def _send_email_notification(alert, recipients_qs=None):
    """Send the alert email via the topology-based recipient set.

    `recipients_qs` is optional, defaults to recipients_for_station_alert
    for the alert's station. The override seam exists for future
    per-channel overrides (e.g., a future "alerts to my secondary
    address" preference). The test-email path does NOT route through
    here — see ``_test_email`` below, which builds its own recipient
    list and calls send_mail directly.
    """
    if recipients_qs is None:
        from apps.monitoring.recipients import email_recipients_for_station_alert

        recipients_qs = email_recipients_for_station_alert(alert.station)

    recipient_list = list(recipients_qs.values_list("email", flat=True))
    if not recipient_list:
        region = alert.station.region.name if alert.station.region else None
        logger.warning(
            "Alert %s on station %s (region=%s) has no recipients. "
            "Configure Station-Admin, Region-Manager, or ensure a "
            "Vereins-Admin has an email set.",
            alert.pk,
            alert.station.name,
            region,
        )
        return

    subject = f"[OE5XRX] {alert.get_severity_display()}: {alert.title}"
    body = (
        f"Station: {alert.station.name}\n"
        f"Severity: {alert.get_severity_display()}\n"
        f"Alert: {alert.title}\n\n"
        f"{alert.message}\n\n"
        f"Time: {alert.created_at}\n"
    )
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        logger.info("Alert email sent to %d recipient(s).", len(recipient_list))
    except Exception:
        logger.exception("Failed to send alert email.")


def _send_webpush_notification(alert):
    """Deliver the alert as Web-Push to PUSH/BOTH users with a device.

    Each subscription is sent in isolation so one dead endpoint never
    blocks the rest (send_web_push prunes 404/410 itself).
    """
    from apps.monitoring.recipients import push_recipients_for_station_alert

    payload = {
        "title": f"[OE5XRX] {alert.get_severity_display()}: {alert.title}",
        "body": f"{alert.station.name}: {alert.message}",
        "url": reverse("monitoring:alert_list"),
        "severity": alert.severity,
    }

    count = 0
    for user in push_recipients_for_station_alert(alert.station):
        for subscription in user.push_subscriptions.all():
            if send_web_push(subscription, payload):
                count += 1
    logger.info("Alert web-push delivered to %d subscription(s).", count)


def _send_telegram_notification(alert):
    """Send alert message via Telegram bot."""
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.warning("Telegram bot token or chat ID not configured.")
        return

    severity_emoji = "\u26a0\ufe0f" if alert.severity == "warning" else "\U0001f6a8"
    message = (
        f"{severity_emoji} *{alert.get_severity_display()}*: {alert.title}\n"
        f"Station: {alert.station.name}\n\n"
        f"{alert.message}"
    )

    try:
        import telegram

        bot = telegram.Bot(token=token)
        bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
        )
        logger.info("Telegram notification sent for alert: %s", alert.title)
    except Exception:
        logger.exception("Failed to send Telegram notification.")


def send_test_notification(channel, requesting_user=None):
    """Send a test notification via the specified channel.

    Args:
        channel: "email" or "telegram"
        requesting_user: the admin who triggered the test (email path
            scopes the mail to this user's address)

    Returns:
        Tuple of (success: bool, error_message: str)
    """
    if channel == "email":
        return _test_email(requesting_user=requesting_user)
    elif channel == "telegram":
        return _test_telegram()
    return False, f"Unknown channel: {channel}"


def _test_email(requesting_user=None):
    """Send a test email to verify SMTP wiring.

    If `requesting_user` is given (the admin who clicked the button),
    the mail goes only to that user's email. This avoids cross-
    notification noise when several admins are configured.
    """
    if not getattr(settings, "ALERT_EMAIL_ENABLED", False):
        return False, "Email notifications are not enabled (ALERT_EMAIL_ENABLED)."

    if requesting_user is not None and requesting_user.email:
        recipient_list = [requesting_user.email]
    else:
        from apps.accounts.models import User as UserModel

        recipient_list = list(
            UserModel.objects.filter(membership_level=UserModel.MembershipLevel.ADMIN)
            .exclude(email="")
            .values_list("email", flat=True)
        )

    if not recipient_list:
        return False, (
            "No recipient — set your user's email or configure a Vereins-Admin with email."
        )

    try:
        send_mail(
            subject="[OE5XRX] Test notification",
            message=(
                f"This is a test notification from OE5XRX Station Manager.\n"
                f"Sent at: {timezone.now()}\n\n"
                f"If you received this, email notifications are working correctly."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        return True, ""
    except Exception as e:
        return False, str(e)


def _test_telegram():
    """Send a test message via Telegram."""
    if not getattr(settings, "ALERT_TELEGRAM_ENABLED", False):
        return False, "Telegram notifications are not enabled (ALERT_TELEGRAM_ENABLED)."

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        return False, "Telegram bot token or chat ID not configured."

    try:
        import telegram

        bot = telegram.Bot(token=token)
        bot.send_message(
            chat_id=chat_id,
            text=(
                "\u2705 *OE5XRX Station Manager*\n"
                "Test notification successful.\n"
                f"Sent at: {timezone.now()}"
            ),
            parse_mode="Markdown",
        )
        return True, ""
    except Exception as e:
        return False, str(e)
