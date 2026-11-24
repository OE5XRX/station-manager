"""Notification dispatch for alerts (email, Telegram, and Web-Push)."""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import formats, timezone
from django.utils.translation import gettext as _
from django.utils.translation import override

from apps.accounts.language import group_by_language, language_for, reverse_for_user
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

    rows = list(recipients_qs.values_list("email", "language"))
    if not rows:
        region = alert.station.region.name if alert.station.region else None
        logger.warning(
            "Alert %s on station %s (region=%s) has no recipients. "
            "Configure Station-Admin, Region-Manager, or ensure a "
            "Club admin has an email set.",
            alert.pk,
            alert.station.name,
            region,
        )
        return

    sent = 0
    for language, group in group_by_language(rows, key=lambda row: row[1]).items():
        recipient_list = [email for email, _language in group]
        try:
            with override(language):
                subject, body = _alert_email(alert)
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipient_list,
                fail_silently=False,
            )
            sent += len(recipient_list)
        except Exception:
            # One failing language group must not cost the others their mail.
            logger.exception("Failed to send alert email (language=%s).", language)
    if sent:
        logger.info("Alert email sent to %d recipient(s).", sent)


def _alert_title(alert) -> str:
    """``[OE5XRX] <severity>: <title>`` in the active language (title stays as stored)."""
    return _("[OE5XRX] %(severity)s: %(title)s") % {
        "severity": alert.get_severity_display(),
        "title": alert.title,
    }


def _local_time(value=None) -> str:
    """Localized date + time with the zone name (e.g. ``CET``) — the reader
    may not be in the server's time zone, and DST makes bare times ambiguous."""
    local = timezone.localtime(value)
    return f"{formats.date_format(local, 'DATETIME_FORMAT')} {local.tzname()}"


def _alert_email(alert):
    """Subject + body of the alert mail in the active language."""
    # A CR/LF in the subject raises a header error and the mail is lost.
    subject = " ".join(_alert_title(alert).splitlines())
    severity = alert.get_severity_display()
    created = _local_time(alert.created_at)
    body = (
        _("Station: %(station)s") % {"station": alert.station.name}
        + "\n"
        + _("Severity: %(severity)s") % {"severity": severity}
        + "\n"
        + _("Alert: %(title)s") % {"title": alert.title}
        + f"\n\n{alert.message}\n\n"
        + _("Time: %(time)s") % {"time": created}
        + "\n"
    )
    return subject, body


def _send_webpush_notification(alert):
    """Deliver the alert as Web-Push to PUSH/BOTH users with a device.

    Each subscription is sent in isolation so one dead endpoint never
    blocks the rest (send_web_push prunes 404/410 itself).
    """
    from apps.monitoring.recipients import push_recipients_for_station_alert

    count = 0
    for user in push_recipients_for_station_alert(alert.station):
        with override(language_for(user)):
            user_payload = {
                "title": _alert_title(alert),
                "body": f"{alert.station.name}: {alert.message}",
                "severity": alert.severity,
                "url": reverse_for_user(user, "monitoring:alert_list"),
            }
        for subscription in user.push_subscriptions.all():
            if send_web_push(subscription, user_payload):
                count += 1
    logger.info("Alert web-push delivered to %d subscription(s).", count)


# Legacy Markdown has no backslash escape: a lone backslash is literal, so it is
# not escaped (escaping it would show a doubled backslash).
_TELEGRAM_MD_SPECIALS = ("_", "*", "`", "[")


def _telegram_md_escape(text) -> str:
    """Escape legacy-Markdown specials in dynamic text.

    Titles/messages embed station names and agent-described metric labels/keys
    (e.g. ``net.eth0.rx_kbps``); an unbalanced ``_``/``*`` makes Telegram reject
    the whole message, silently dropping the alert.
    """
    text = str(text)
    for ch in _TELEGRAM_MD_SPECIALS:
        text = text.replace(ch, f"\\{ch}")
    return text


def _send_telegram_notification(alert):
    """Send alert message via Telegram bot."""
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.warning("Telegram bot token or chat ID not configured.")
        return

    severity_emoji = "\u26a0\ufe0f" if alert.severity == "warning" else "\U0001f6a8"
    # Telegram goes to one shared chat: write it in the site language.
    with override(settings.LANGUAGE_CODE):
        message = (
            f"{severity_emoji} *{alert.get_severity_display()}*: "
            f"{_telegram_md_escape(alert.title)}\n"
            + _("Station: %(station)s") % {"station": _telegram_md_escape(alert.station.name)}
            + f"\n\n{_telegram_md_escape(alert.message)}"
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
        channel: "email", "telegram", or "webpush"
        requesting_user: the admin who triggered the test (email and
            web-push paths scope the notification to this user only —
            their address / their own registered devices)

    Returns:
        Tuple of (success: bool, error_message: str)
    """
    if channel == "email":
        return _test_email(requesting_user=requesting_user)
    elif channel == "telegram":
        return _test_telegram()
    elif channel == "webpush":
        return _test_webpush(requesting_user=requesting_user)
    return False, _("Unknown channel: %(channel)s") % {"channel": channel}


def _test_webpush(requesting_user=None):
    """Send a test Web-Push to the requesting user's own devices.

    Scoped to the clicking user's registered subscriptions (like
    ``_test_email`` scopes to their address) so testing never pushes to
    other admins' phones. Returns a clear error when the channel is
    unconfigured or the user has no working device.
    """
    if not getattr(settings, "ALERT_WEBPUSH_ENABLED", False):
        return False, _("Web-Push is not enabled (no VAPID keys configured).")

    if requesting_user is None:
        return False, _("No requesting user to send the test push to.")

    subscriptions = list(requesting_user.push_subscriptions.all())
    if not subscriptions:
        return False, _(
            "No push device registered — open this app from the home screen "
            "and enable push on this device first."
        )

    # Recipient-facing text in the recipient's language; the error strings
    # returned to the UI stay in the request language (outside the override).
    with override(language_for(requesting_user)):
        payload = {
            "title": _("[OE5XRX] Test notification"),
            "body": _("Web-Push is working correctly."),
            "url": reverse_for_user(requesting_user, "monitoring:alert_list"),
        }
    sent = sum(1 for sub in subscriptions if send_web_push(sub, payload))
    if sent == 0:
        return False, _(
            "Push delivery failed for all your registered devices — a device "
            "subscription may have expired (re-enable push here), or the push "
            "service rejected the request (check the VAPID config and server logs)."
        )
    return True, ""


def _test_email(requesting_user=None):
    """Send a test email to verify SMTP wiring.

    If `requesting_user` is given (the admin who clicked the button),
    the mail goes only to that user's email. This avoids cross-
    notification noise when several admins are configured.
    """
    if not getattr(settings, "ALERT_EMAIL_ENABLED", False):
        return False, _("Email notifications are not enabled (ALERT_EMAIL_ENABLED).")

    if requesting_user is not None and requesting_user.email:
        groups = {language_for(requesting_user): [requesting_user.email]}
    else:
        from apps.accounts.permissions import users_with

        rows = (
            users_with("alert_rules", "manage").exclude(email="").values_list("email", "language")
        )
        groups = {
            language: [email for email, _language in group]
            for language, group in group_by_language(rows, key=lambda row: row[1]).items()
        }

    if not groups:
        return False, _(
            "No recipient — set your user's email or configure a Club admin with email."
        )

    errors = []
    for language, recipient_list in groups.items():
        with override(language):
            subject = _("[OE5XRX] Test notification")
            message = _(
                "This is a test notification from OE5XRX Station Manager.\n"
                "Sent at: %(time)s\n\n"
                "If you received this, email notifications are working correctly."
            ) % {"time": _local_time()}
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipient_list,
                fail_silently=False,
            )
        except Exception as e:
            # Name the group: with several languages one may succeed.
            errors.append(f"{language}: {e}")
    if errors:
        return False, "; ".join(errors)
    return True, ""


def _test_telegram():
    """Send a test message via Telegram."""
    if not getattr(settings, "ALERT_TELEGRAM_ENABLED", False):
        return False, _("Telegram notifications are not enabled (ALERT_TELEGRAM_ENABLED).")

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        return False, _("Telegram bot token or chat ID not configured.")

    with override(settings.LANGUAGE_CODE):
        text = "\u2705 *OE5XRX Station Manager*\n" + _(
            "Test notification successful.\nSent at: %(time)s"
        ) % {"time": _local_time()}

    try:
        import telegram

        bot = telegram.Bot(token=token)
        bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        return True, ""
    except Exception as e:
        return False, str(e)
