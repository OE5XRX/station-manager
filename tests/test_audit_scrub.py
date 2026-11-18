"""scrub_message: removes e-mail addresses, IPs and legacy UA/GeoIP fragments."""

import pytest

from apps.audit.scrub import EMAIL_PLACEHOLDER, IP_PLACEHOLDER, scrub_message


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("to oe5abc@example.org from 192.0.2.10", "to [email] from [ip]"),
        ("OE5ABC <a.b+c@mail.example.at>", "OE5ABC <[email]>"),
        ("user 2001:db8::1 and ::1", "user [ip] and [ip]"),
        ("Token issued. UA=Mozilla/5.0 (X11) City=Linz", "Token issued."),
        ("Token issued. UA=Mozilla/5.0\nsecond line City=Linz", "Token issued."),
        (
            "Session 7 revoked. Issued 2026-01-01T12:30:45+00:00 from 192.0.2.1 (Linz)",
            "Session 7 revoked. Issued 2026-01-01T12:30:45+00:00",
        ),
        (
            "Session 7 revoked. Issued 2026-01-01T12:30:45+00:00 from None (unknown)",
            "Session 7 revoked. Issued 2026-01-01T12:30:45+00:00",
        ),
        ("firmware v1.2.3.4 at 12:30:45", "firmware v1.2.3.4 at 12:30:45"),
        ("version 1.2.3", "version 1.2.3"),
        ("from 192.0.2.1:8080", "from [ip]:8080"),
        ("IP:10.0.0.1", "IP:[ip]"),
        ("ip=1.2.3.4:8080", "ip=[ip]:8080"),
        ("contact mü@example.at", "contact [email]"),
        (
            "Policy grant_required -> open. 2 active session(s)",
            "Policy grant_required -> open. 2 active session(s)",
        ),
        ("IP address changed: 10.0.0.1 → 10.0.0.2", "IP address changed: [ip] → [ip]"),
        ("user (192.0.2.4)", "user ([ip])"),
        ("Image imported from v1.2 (release)", "Image imported from v1.2 (release)"),
        ("reset requested from 192.0.2.1.", "reset requested from [ip]."),
        ("mac aa:bb:cc:dd:ee:ff stays", "mac aa:bb:cc:dd:ee:ff stays"),
        ("", ""),
    ],
)
def test_scrub_message(raw, clean):
    assert scrub_message(raw) == clean


@pytest.mark.parametrize(
    "raw",
    [
        "to oe5abc@example.org from 192.0.2.10",
        "Token issued. UA=Mozilla/5.0 (X11) City=Linz",
        "Session 7 revoked. Issued 2026-01-01T12:30:45+00:00 from 192.0.2.1 (Linz)",
        "user 2001:db8::1 and ::1",
        "firmware v1.2.3.4 at 12:30:45",
    ],
)
def test_scrub_is_idempotent(raw):
    once = scrub_message(raw)
    assert scrub_message(once) == once


def test_placeholders():
    assert EMAIL_PLACEHOLDER == "[email]"
    assert IP_PLACEHOLDER == "[ip]"


def test_scrub_long_input_is_linear():
    import time

    for text in ("a" * 40000, "a@" * 20000, "a." * 10000 + "@" + "b." * 10000):
        start = time.monotonic()
        scrub_message(text)
        assert time.monotonic() - start < 1.0


def test_unclosed_legacy_from_runs_are_linear():
    import time

    start = time.monotonic()
    scrub_message(" from x (" * 20000)
    assert time.monotonic() - start < 1.0
