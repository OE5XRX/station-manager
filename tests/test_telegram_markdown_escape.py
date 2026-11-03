"""Telegram alerts escape dynamic text so legacy Markdown never rejects them."""

import sys
import types

import pytest

from apps.monitoring.models import Alert
from apps.monitoring.notifications import _send_telegram_notification, _telegram_md_escape


def test_escape_specials():
    assert _telegram_md_escape("net.eth0.rx_kbps *x* `y` [z]") == (
        "net.eth0.rx\\_kbps \\*x\\* \\`y\\` \\[z]"
    )


@pytest.mark.django_db
def test_telegram_message_escapes_metric_key_title(station, settings, monkeypatch):
    settings.TELEGRAM_BOT_TOKEN = "t"
    settings.TELEGRAM_CHAT_ID = "c"
    sent = {}

    class FakeBot:
        def __init__(self, token):
            pass

        def send_message(self, **kwargs):
            sent.update(kwargs)

    monkeypatch.setitem(sys.modules, "telegram", types.SimpleNamespace(Bot=FakeBot))
    alert = Alert.objects.create(
        station=station,
        severity=Alert.Severity.WARNING,
        title="net.eth0.rx_kbps: st",
        message="net.eth0.rx_kbps on st: 5 above 1.",
    )
    _send_telegram_notification(alert)
    assert "rx\\_kbps" in sent["text"]
    assert "rx_kbps" not in sent["text"].replace("rx\\_kbps", "")
    assert sent["parse_mode"] == "Markdown"
