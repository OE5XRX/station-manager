"""Notification dispatch for alerts (email and Telegram)."""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.accounts.models import User

logger = logging.getLogger(__name__)


def send_alert_notifications(alert):
    """Send notifications for a new alert to all admin users."""
    admins = User.objects.filter(role=User.Role.ADMIN)
    if not admins.exists():
        logger.warning("No admin users found for alert notifications.")
        return

    if getattr(settings, "ALERT_EMAIL_ENABLED", False):
        _send_email_notification(alert, admins)

    if getattr(settings, "ALERT_TELEGRAM_ENABLED", False):
        _send_telegram_notification(alert)


def _send_email_notification(alert, admins):
    """Send alert email to all admin users."""
    subject = f"[OE5XRX] {alert.get_severity_display()}: {alert.title}"
    body = (
        f"Station: {alert.station.name}\n"
        f"Severity: {alert.get_severity_display()}\n"
        f"Alert: {alert.title}\n\n"
        f"{alert.message}\n\n"
        f"Time: {alert.created_at}\n"
    )

    recipient_list = [admin.email for admin in admins if admin.email]
    if not recipient_list:
        logger.warning("No admin users have email addresses configured.")
        return

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


def send_test_notification(channel):
    """Send a test notification via the specified channel.

    Args:
        channel: "email" or "telegram"

    Returns:
        Tuple of (success: bool, error_message: str)
    """
    if channel == "email":
        return _test_email()
    elif channel == "telegram":
        return _test_telegram()
    return False, f"Unknown channel: {channel}"


def _test_email():
    """Send a test email to all admin users."""
    if not getattr(settings, "ALERT_EMAIL_ENABLED", False):
        return False, "Email notifications are not enabled (ALERT_EMAIL_ENABLED)."

    admins = User.objects.filter(role=User.Role.ADMIN)
    recipient_list = [admin.email for admin in admins if admin.email]
    if not recipient_list:
        return False, "No admin users have email addresses configured."

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
