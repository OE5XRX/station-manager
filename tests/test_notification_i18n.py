"""Alert + test notifications are written in each recipient's language.

Stored texts (alert title/message, station name) stay as they are; only the
framing (subject, labels, push title) is translated. Telegram goes to a shared
chat and uses ``settings.LANGUAGE_CODE``. UI error strings returned by
``send_test_notification`` follow the *active* (request) language.
"""

import sys
import types
from unittest import mock

import pytest
from django.core import mail
from django.utils import timezone, translation

from apps.accounts.models import User
from apps.monitoring.models import Alert, AlertRule
from apps.monitoring.notifications import send_alert_notifications, send_test_notification
from apps.stations.models import Station
from apps.webpush.models import PushSubscription


def _admin(username, language="en", channel=User.NotifyChannel.EMAIL, email=None):
    u = User.objects.create_user(
        username=username, password="x", email=f"{username}@x" if email is None else email
    )
    u.membership_level = User.MembershipLevel.ADMIN
    u.notify_channel = channel
    u.save(update_fields=["membership_level", "notify_channel"])
    # Bypass choices validation so unsupported codes can be tested too.
    User.objects.filter(pk=u.pk).update(language=language)
    u.language = language
    return u


def _alert(severity="warning"):
    station = Station.objects.create(name="OE5A", callsign="OE5A")
    rule = AlertRule.objects.get(alert_type=AlertRule.AlertType.STATION_OFFLINE)
    return Alert.objects.create(
        station=station, alert_rule=rule, severity=severity, title="CPU hot", message="m1"
    )


@pytest.fixture
def email_on(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.ALERT_TELEGRAM_ENABLED = False
    settings.ALERT_WEBPUSH_ENABLED = False
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    return settings


def _by_recipients(outbox):
    return {frozenset(m.to): m for m in outbox}


@pytest.mark.django_db
def test_alert_email_one_mail_per_language(email_on):
    _admin("de1", "de")
    _admin("en1", "en")
    send_alert_notifications(_alert("warning"))

    assert len(mail.outbox) == 2
    by_to = _by_recipients(mail.outbox)
    de_mail = by_to[frozenset({"de1@x"})]
    en_mail = by_to[frozenset({"en1@x"})]

    assert de_mail.subject == "[OE5XRX] Warnung: CPU hot"
    assert "Station: OE5A" in de_mail.body
    assert "Schweregrad: Warnung" in de_mail.body
    assert "Alarm: CPU hot" in de_mail.body
    assert "Zeit:" in de_mail.body
    assert "m1" in de_mail.body

    assert en_mail.subject == "[OE5XRX] Warning: CPU hot"
    assert "Severity: Warning" in en_mail.body
    assert "Alert: CPU hot" in en_mail.body
    assert "Time:" in en_mail.body


@pytest.mark.django_db
def test_alert_email_critical_label_german(email_on):
    _admin("de1", "de")
    send_alert_notifications(_alert("critical"))
    assert mail.outbox[0].subject == "[OE5XRX] Kritisch: CPU hot"


@pytest.mark.django_db
def test_alert_email_shared_language_is_one_mail(email_on):
    _admin("de1", "de")
    _admin("de2", "de")
    _admin("en1", "en")
    send_alert_notifications(_alert())
    assert len(mail.outbox) == 2
    by_to = _by_recipients(mail.outbox)
    assert by_to[frozenset({"de1@x", "de2@x"})].subject.startswith("[OE5XRX] Warnung")


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["xx", ""])
def test_alert_email_unsupported_language_falls_back(email_on, bad):
    email_on.LANGUAGE_CODE = "en"
    _admin("odd", bad)
    _admin("en1", "en")
    send_alert_notifications(_alert())
    assert len(mail.outbox) == 1
    assert set(mail.outbox[0].to) == {"odd@x", "en1@x"}
    assert mail.outbox[0].subject == "[OE5XRX] Warning: CPU hot"


@pytest.mark.django_db
def test_alert_email_failure_in_one_group_does_not_block_other(email_on, caplog):
    from django.core.mail import send_mail as real_send_mail

    _admin("de1", "de")
    _admin("en1", "en")

    def flaky(*, recipient_list, **kwargs):
        if "de1@x" in recipient_list:
            raise OSError("smtp down")
        return real_send_mail(recipient_list=recipient_list, **kwargs)

    with mock.patch("apps.monitoring.notifications.send_mail", side_effect=flaky):
        send_alert_notifications(_alert())

    assert [m.to for m in mail.outbox] == [["en1@x"]]
    assert any(
        r.levelname == "ERROR" and r.exc_info and "smtp down" in str(r.exc_info[1])
        for r in caplog.records
    )


@pytest.mark.django_db
def test_alert_push_payload_in_each_users_language(settings):
    settings.ALERT_EMAIL_ENABLED = False
    settings.ALERT_TELEGRAM_ENABLED = False
    settings.ALERT_WEBPUSH_ENABLED = True
    de = _admin("de1", "de", User.NotifyChannel.PUSH)
    en = _admin("en1", "en", User.NotifyChannel.PUSH)
    for u in (de, en):
        PushSubscription.objects.create(
            user=u, endpoint=f"https://push.example/{u.pk}", p256dh="p", auth="a"
        )
    with mock.patch("apps.monitoring.notifications.send_web_push", return_value=True) as m:
        send_alert_notifications(_alert())

    payloads = {call.args[0].user_id: call.args[1] for call in m.call_args_list}
    assert payloads[de.pk]["title"] == "[OE5XRX] Warnung: CPU hot"
    assert payloads[de.pk]["url"].startswith("/de/")
    assert payloads[de.pk]["body"] == "OE5A: m1"
    assert payloads[de.pk]["severity"] == "warning"
    assert payloads[en.pk]["title"] == "[OE5XRX] Warning: CPU hot"
    assert payloads[en.pk]["url"].startswith("/en/")


def _fake_telegram():
    bot = mock.MagicMock()
    module = types.ModuleType("telegram")
    module.Bot = mock.MagicMock(return_value=bot)
    return module, bot


@pytest.fixture
def telegram_on(settings):
    settings.ALERT_EMAIL_ENABLED = False
    settings.ALERT_WEBPUSH_ENABLED = False
    settings.ALERT_TELEGRAM_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "t"
    settings.TELEGRAM_CHAT_ID = "c"
    return settings


@pytest.mark.django_db
def test_alert_telegram_uses_language_code_not_active_language(telegram_on):
    telegram_on.LANGUAGE_CODE = "en"
    module, bot = _fake_telegram()
    alert = _alert()
    with mock.patch.dict(sys.modules, {"telegram": module}), translation.override("de"):
        send_alert_notifications(alert)
    text = bot.send_message.call_args.kwargs["text"]
    assert "*Warning*" in text
    assert "Station: OE5A" in text
    assert "Warnung" not in text


@pytest.mark.django_db
def test_alert_telegram_german_when_language_code_is_de(telegram_on):
    telegram_on.LANGUAGE_CODE = "de"
    module, bot = _fake_telegram()
    alert = _alert()
    with mock.patch.dict(sys.modules, {"telegram": module}), translation.override("en"):
        send_alert_notifications(alert)
    assert "*Warnung*" in bot.send_message.call_args.kwargs["text"]


@pytest.mark.django_db
def test_dispatch_does_not_leak_active_language(email_on, telegram_on):
    email_on.ALERT_EMAIL_ENABLED = True
    _admin("de1", "de")
    _admin("en1", "en")
    module, _bot = _fake_telegram()
    alert = _alert()
    with mock.patch.dict(sys.modules, {"telegram": module}):
        with translation.override("en"):
            send_alert_notifications(alert)
            assert translation.get_language() == "en"
        with translation.override("de"):
            send_alert_notifications(alert)
            assert translation.get_language() == "de"


# --- test notifications -----------------------------------------------------


@pytest.mark.django_db
def test_test_email_in_requesting_users_language(email_on):
    de = _admin("de1", "de")
    with translation.override("en"):
        ok, err = send_test_notification("email", requesting_user=de)
        assert translation.get_language() == "en"
    assert ok is True and err == ""
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["de1@x"]
    assert mail.outbox[0].subject == "[OE5XRX] Testbenachrichtigung"
    assert "Testbenachrichtigung" in mail.outbox[0].body


@pytest.mark.django_db
def test_test_email_fallback_admins_grouped_by_language(email_on):
    _admin("de1", "de")
    _admin("en1", "en")
    nomail = _admin("nomail", "en", email="")
    ok, _err = send_test_notification("email", requesting_user=nomail)
    assert ok is True
    by_to = _by_recipients(mail.outbox)
    assert by_to[frozenset({"de1@x"})].subject == "[OE5XRX] Testbenachrichtigung"
    assert by_to[frozenset({"en1@x"})].subject == "[OE5XRX] Test notification"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("active", "expected"),
    [("de", "nicht aktiviert"), ("en", "not enabled")],
)
def test_test_email_disabled_error_in_active_language(settings, active, expected):
    settings.ALERT_EMAIL_ENABLED = False
    de = _admin("de1", "de")
    with translation.override(active):
        ok, err = send_test_notification("email", requesting_user=de)
    assert ok is False
    assert expected in err


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("active", "expected"),
    [("de", "Kein Empfänger"), ("en", "No recipient")],
)
def test_test_email_no_recipient_error_in_active_language(email_on, active, expected):
    # The only admin (the requester, a German user) has no e-mail address.
    de = _admin("de1", "de", email="")
    with translation.override(active):
        ok, err = send_test_notification("email", requesting_user=de)
    assert ok is False
    assert expected in err


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("active", "expected"),
    [("de", "Kein Push-Gerät"), ("en", "No push device")],
)
def test_test_push_no_device_error_in_active_language(settings, active, expected):
    settings.ALERT_WEBPUSH_ENABLED = True
    en = _admin("en1", "en")
    with translation.override(active):
        ok, err = send_test_notification("webpush", requesting_user=en)
    assert ok is False
    assert expected in err


@pytest.mark.django_db
def test_test_push_payload_in_requesting_users_language(settings):
    settings.ALERT_WEBPUSH_ENABLED = True
    de = _admin("de1", "de")
    PushSubscription.objects.create(
        user=de, endpoint="https://push.example/de", p256dh="p", auth="a"
    )
    with (
        translation.override("en"),
        mock.patch("apps.monitoring.notifications.send_web_push", return_value=True) as m,
    ):
        ok, _err = send_test_notification("webpush", requesting_user=de)
        assert translation.get_language() == "en"
    assert ok is True
    payload = m.call_args.args[1]
    assert payload["title"] == "[OE5XRX] Testbenachrichtigung"
    assert payload["body"] == "Web-Push funktioniert."
    assert payload["url"].startswith("/de/")


@pytest.mark.django_db
def test_test_telegram_uses_language_code(telegram_on):
    telegram_on.LANGUAGE_CODE = "en"
    module, bot = _fake_telegram()
    with mock.patch.dict(sys.modules, {"telegram": module}), translation.override("de"):
        ok, _err = send_test_notification("telegram")
    assert ok is True
    text = bot.send_message.call_args.kwargs["text"]
    assert "Test notification successful." in text


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("active", "expected"),
    [("de", "nicht aktiviert"), ("en", "not enabled")],
)
def test_test_telegram_disabled_error_in_active_language(settings, active, expected):
    settings.ALERT_TELEGRAM_ENABLED = False
    with translation.override(active):
        ok, err = send_test_notification("telegram")
    assert ok is False
    assert expected in err


def test_unknown_channel_error_is_translated():
    with translation.override("de"):
        ok, err = send_test_notification("pigeon")
    assert ok is False
    assert "pigeon" in err
    assert "Unbekannter Kanal" in err


@pytest.mark.django_db
def test_alert_email_render_failure_in_one_group_does_not_block_other(email_on, caplog):
    """A rendering error (not just SMTP) for one language stays isolated."""
    from apps.monitoring import notifications

    _admin("de1", "de")
    _admin("en1", "en")
    real = notifications._alert_email

    def flaky(alert):
        if translation.get_language() == "de":
            raise ValueError("bad catalog")
        return real(alert)

    with mock.patch.object(notifications, "_alert_email", side_effect=flaky):
        send_alert_notifications(_alert())

    assert [m.to for m in mail.outbox] == [["en1@x"]]
    assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)


@pytest.mark.django_db
def test_alert_email_time_carries_timezone(email_on, settings):
    settings.TIME_ZONE = "Europe/Vienna"
    _admin("en1", "en")
    alert = _alert()
    send_alert_notifications(alert)
    tzname = timezone.localtime(alert.created_at).tzname()
    assert tzname in ("CET", "CEST")
    time_line = next(line for line in mail.outbox[0].body.splitlines() if line.startswith("Time:"))
    assert time_line.endswith(f" {tzname}")


@pytest.mark.django_db
def test_test_email_partial_failure_names_the_failing_language(email_on):
    from django.core.mail import send_mail as real_send_mail

    nomail = _admin("nomail", "de", email="")
    _admin("de1", "de")
    _admin("en1", "en")

    def flaky(*, recipient_list, **kwargs):
        if "de1@x" in recipient_list:
            raise OSError("smtp down")
        return real_send_mail(recipient_list=recipient_list, **kwargs)

    with mock.patch("apps.monitoring.notifications.send_mail", side_effect=flaky):
        ok, err = send_test_notification("email", requesting_user=nomail)

    assert ok is False
    assert err == "de: smtp down"
    assert [m.to for m in mail.outbox] == [["en1@x"]]
