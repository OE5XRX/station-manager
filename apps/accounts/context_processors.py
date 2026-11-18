"""Template context: permission registry (``can.<area>.<verb>``) and privacy-policy link."""

from django.conf import settings

from apps.accounts.permissions import AREAS, VERBS, can


class _AreaProxy:
    __slots__ = ("_user", "_area", "_cache")

    def __init__(self, user, area):
        self._user, self._area, self._cache = user, area, {}

    def __getattr__(self, verb):
        if verb not in VERBS:
            raise AttributeError(verb)
        if verb not in self._cache:
            self._cache[verb] = can(self._user, self._area, verb)
        return self._cache[verb]


class CanProxy:
    __slots__ = ("_user", "_areas")

    def __init__(self, user):
        self._user, self._areas = user, {}

    def __getitem__(self, area):
        if area not in AREAS:
            raise KeyError(area)
        if area not in self._areas:
            self._areas[area] = _AreaProxy(self._user, area)
        return self._areas[area]


def permissions(request):
    return {"can": CanProxy(getattr(request, "user", None))}


def privacy(request):
    """Privacy-policy URL/version for the footer link (hidden while version is 0)."""
    return {
        "PRIVACY_POLICY_URL": settings.PRIVACY_POLICY_URL,
        "PRIVACY_POLICY_VERSION": settings.PRIVACY_POLICY_VERSION,
    }
