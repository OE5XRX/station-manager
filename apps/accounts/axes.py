"""django-axes integration."""

from django.conf import settings


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
