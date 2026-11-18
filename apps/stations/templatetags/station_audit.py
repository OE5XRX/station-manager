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
# IPv4 dotted quads and IPv6 (full form or any "::" compressed form).
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|(?<![\w:])(?:" + _IPV6 + r")(?![\w:])")


@register.filter
def mask_ips(value):
    """Replace IP addresses in a free-text audit message with a placeholder.

    Display-only: the stored message is never modified.
    """
    return _IP_RE.sub("[IP]", str(value))
