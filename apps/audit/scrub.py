"""Remove personal data (e-mail, IP, legacy UA/GeoIP fragments) from audit free text.

Audit messages are kept for ``RETENTION_AUDIT_DAYS`` (365 d), IPs only for
``RETENTION_IP_DAYS`` (90 d). New messages no longer contain e-mail/IP/UA/city;
``scrub_message`` cleans legacy rows (and old station IP-change messages) so the
free text does not outlive the 90-day IP rule. Pure and idempotent.
"""

import ipaddress
import re

from django.db.models import Q

EMAIL_PLACEHOLDER = "[email]"
IP_PLACEHOLDER = "[ip]"

# Coarse DB pre-selection for rows that *might* need scrubbing. The exact
# decision is made in Python by comparing scrub_message(message) to message.
SCRUB_PREFILTER = (
    Q(message__contains="@")
    | Q(message__regex=r"[0-9]\.[0-9]+\.[0-9]")
    | Q(message__contains="::")
    | Q(message__regex=r"[0-9A-Fa-f]:[0-9A-Fa-f]")
    | Q(message__startswith="Token issued. UA=")
    | Q(message__contains=" from ")
)

# Legacy LOGIN_SUCCESS: "Token issued. UA=<ua> City=<city>".
_LEGACY_LOGIN_RE = re.compile(r"^Token issued\. UA=.*$", re.DOTALL)
# Legacy SESSION_REVOKED tail: " from <ip> (<city>)". Only stripped when the
# token is an IP (or "None" — a session without recorded IP), so ordinary
# prose like "copied from foo (bar)" is left alone.
_LEGACY_FROM_RE = re.compile(r" from (\S+) \([^)]*\)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IP_CANDIDATE_RE = re.compile(r"(?<![\w.:])[0-9A-Fa-f:.]{2,}(?![\w:])")


def _is_ip(token: str) -> bool:
    try:
        ipaddress.ip_address(token)
    except ValueError:
        return False
    return True


def _strip_legacy_from(match: re.Match) -> str:
    token = match.group(1)
    return "" if token == "None" or _is_ip(token) else match.group(0)


def _replace_ip(match: re.Match) -> str:
    token = match.group(0)
    if "." not in token and ":" not in token:
        return token
    if _is_ip(token):
        return IP_PLACEHOLDER
    # Sentence-final IP ("... from 192.0.2.1."): the trailing dot is prose.
    stripped = token.rstrip(".")
    if stripped != token and _is_ip(stripped):
        return IP_PLACEHOLDER + token[len(stripped) :]
    return token


def scrub_message(text: str) -> str:
    """Return ``text`` with e-mails/IPs replaced and legacy UA/city fragments removed."""
    if not text:
        return text
    text = _LEGACY_LOGIN_RE.sub("Token issued.", text)
    text = _LEGACY_FROM_RE.sub(_strip_legacy_from, text)
    text = _EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    return _IP_CANDIDATE_RE.sub(_replace_ip, text)
