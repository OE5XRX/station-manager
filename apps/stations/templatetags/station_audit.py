import re

from django import template

register = template.Library()

_H = "[0-9A-Fa-f]{1,4}"
_IPV6 = "|".join(
    [
        "(?:" + _H + ":){7}" + _H,
        "(?:" + _H + ":){1,7}:(?:" + _H + "(?::" + _H + "){0,6})?",
        "::(?:" + _H + ":){0,6}" + _H,
        "::",
    ]
)
_V4 = r"\d{1,3}(?:\.\d{1,3}){3}"
# IPv4 dotted quads, IPv4-embedded IPv6 (::ffff:1.2.3.4), and IPv6 (full or "::" form).
# Deliberately fail-closed: hex-only words around "::" and 4-part versions also match.
_IP_RE = re.compile(
    r"(?<![\w:.])[0-9A-Fa-f:]*:" + _V4 + r"\b"
    r"|\b" + _V4 + r"\b"
    r"|(?<![\w:])(?:" + _IPV6 + r")(?![\w:])"
)


@register.filter
def mask_ips(value):
    """Replace IP addresses in a free-text audit message with a placeholder.

    Display-only: the stored message is never modified.
    """
    return _IP_RE.sub("[IP]", str(value))
