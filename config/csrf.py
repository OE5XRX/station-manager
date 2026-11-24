"""Friendly handling of CSRF failures (``CSRF_FAILURE_VIEW``).

The typical cause is a stale tab: the user logs in (or sets a password,
which auto-logs-in) in one tab, the CSRF token rotates, and a second tab
still holds the old token. Django's default answer is a raw 403 page.

For the auth forms we answer with a *GET* redirect instead. The rejected
POST is never processed, so CSRF protection is not weakened. Every other
path keeps a 403, rendered in the app layout.
"""

from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.middleware.csrf import REASON_NO_CSRF_COOKIE
from django.shortcuts import render, resolve_url
from django.urls import Resolver404, resolve, reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.translation import gettext as _

# URL names (``app:name``) of the forms an anonymous or freshly logged-in
# user may submit from an outdated tab.
AUTH_FORM_VIEWS = frozenset(
    {
        "accounts:login",
        "accounts:set_password",
        "accounts:password_reset_request",
    }
)


def _safe_next(request) -> str:
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return ""


def wants_reauth(next_url: str) -> bool:
    """True if ``next`` is the OIDC authorize endpoint.

    DOT's ``prompt=login`` sends an *already logged-in* user to the login
    page with ``next`` = authorize (``prompt`` stripped); the login form
    must then be shown, not skipped, or the forced re-auth is bypassed.
    """
    try:
        return resolve(urlsplit(next_url).path).url_name == "sso-authorize"
    except Resolver404:
        return False


# Only stale/missing-token failures are a "session expired" case. Origin /
# Referer failures (e.g. a proxy or CSRF_TRUSTED_ORIGINS misconfiguration)
# keep the 403 so they are not masked as a harmless expiry.
def _is_token_failure(reason: str) -> bool:
    # Django's token reasons: "CSRF token missing." and
    # "CSRF token from POST/header incorrect / has incorrect length / ...".
    return reason == REASON_NO_CSRF_COOKIE or reason.startswith("CSRF token")


def csrf_failure(request, reason="", template_name="403_csrf.html"):
    try:
        view_name = resolve(request.path_info).view_name
    except Resolver404:
        view_name = ""

    if view_name in AUTH_FORM_VIEWS and _is_token_failure(reason):
        next_url = _safe_next(request)
        if request.user.is_authenticated and not wants_reauth(next_url):
            return HttpResponseRedirect(next_url or resolve_url(settings.LOGIN_REDIRECT_URL))
        if not request.user.is_authenticated:
            messages.warning(
                request,
                _("The session has expired – please try again."),
            )
        if view_name == "accounts:login":
            target = reverse("accounts:login")
            if next_url:
                target += "?" + urlencode({"next": next_url})
            return HttpResponseRedirect(target)
        return HttpResponseRedirect(request.path)

    return render(request, template_name, {"reason": reason}, status=403)
