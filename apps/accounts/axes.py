"""django-axes integration."""

import ipaddress

from django.conf import settings

from apps.accounts.client_ip import client_ip

# One IPv6 subscriber typically gets a whole /64; keying on the full address
# would hand every address in it a fresh lockout budget.
_IPV6_LOCKOUT_PREFIX = 64


def get_axes_client_ip(request):
    """Lockout IP: the trusted client IP, IPv6 collapsed to its /64.

    Same trusted source as the audit rows (``apps.accounts.client_ip``) — axes'
    default ``REMOTE_ADDR`` is the cloudflared container in prod, i.e. one
    shared IP for every client. The /64 network address is still a valid
    address for axes' ``GenericIPAddressField``.

    Trade-off: axes also stores this value in ``AccessAttempt``/``AccessLog``,
    so those logs hold the /64, not the full IPv6 (hosts within one prefix are
    indistinguishable there; data-minimising). Audit rows keep the full IP.
    """
    ip = client_ip(request)
    if ip is None or ":" not in ip:
        return ip
    addr = ipaddress.IPv6Address(ip)
    if addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(ipaddress.IPv6Network(f"{ip}/{_IPV6_LOCKOUT_PREFIX}", strict=False).network_address)


def get_axes_username(request, credentials=None):
    """Lockout key: the submitted username, uppercased.

    Mirrors axes' default resolution (credentials, then request data) but
    upper-cases the result (the stored form; the DB's ``iexact`` compares via
    ``UPPER()``, so e.g. ``ı`` -> ``I`` cannot dodge the key) so ``OE5ABC`` /
    ``oe5abc`` share one counter — login itself is case-insensitive, so they are the same account.
    """
    field = settings.AXES_USERNAME_FORM_FIELD
    if credentials:
        username = credentials.get(field)
    else:
        data = getattr(request, "data", request.POST)
        username = data.get(field)
    return username.upper() if isinstance(username, str) else username
