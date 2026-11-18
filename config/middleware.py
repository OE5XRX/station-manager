"""Project-wide middleware customisations.

* ``LoginRequiredMiddleware`` — our subclass of Django 5.1+'s
  ``django.contrib.auth.middleware.LoginRequiredMiddleware`` that bolts a
  two-part allow-list (exact paths + true prefixes) onto the framework's
  default-deny gate.
* ``PrivacyAckMiddleware`` — sends logged-in users who have not yet
  acknowledged the current privacy-policy version to the acknowledgement
  page (browser UI only).

See the docstrings on the classes for the rationale.
"""

import posixpath
from typing import Final
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.middleware import (
    LoginRequiredMiddleware as DjangoLoginRequiredMiddleware,
)
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin
from django_htmx.http import HttpResponseClientRedirect


def normalise_path(path: str) -> str:
    """Normalise ``path`` for allow-list matching.

    ``posixpath.normpath`` collapses ``..`` segments and duplicate inner
    slashes, so a traversal like ``/sso/token/../authorize/`` cannot flip
    a gate decision. Two gotchas are handled: normpath preserves a leading
    ``//`` (collapsed to a single ``/`` here) and drops the trailing ``/``
    (re-appended, so every result ends with ``/``).
    """
    normalised = posixpath.normpath(path)
    # Collapse leading double-slashes that posixpath preserves.
    normalised = "/" + normalised.lstrip("/")
    if not normalised.endswith("/"):
        normalised += "/"
    return normalised


class LoginRequiredMiddleware(DjangoLoginRequiredMiddleware):
    """Default-deny middleware with an allow-list for unowned endpoints.

    Django's ``LoginRequiredMiddleware`` (Django 5.1+) enforces
    no-anonymous-access globally and respects the
    ``django.contrib.auth.decorators.login_not_required`` marker on
    individual views. That works for views we own — we just decorate
    them. It does *not* work for the OAuth/OIDC public surface that
    lives in ``django-oauth-toolkit``: those views ship without the
    marker and subclassing each one in our URL conf would be six
    re-implementations of one-liners (token, revoke, introspect,
    userinfo, RP-initiated logout, JWKS / discovery).

    Instead we maintain a two-part allow-list here:

    * :pyattr:`PUBLIC_EXACT_PATHS` — exact-match entries for
      single-endpoint exemptions (``/sso/token/``, ``/sso/userinfo/``,
      …). A future view added beneath one of these (e.g.
      ``/sso/token/audit/``) does **not** silently inherit anonymous
      access.
    * :pyattr:`PUBLIC_PATH_PREFIXES` — true-prefix entries reserved
      for genuine registries whose member URIs are bounded by an
      external spec (currently only ``/sso/.well-known/``).

    Trade-offs:

    * Pro: a *single* place in the code base declares which OIDC/OAuth
      paths are public. Adding a new public endpoint is one line.
    * Pro: the third-party views need no patching; an upstream
      django-oauth-toolkit refactor can land without touching us.
    * Con: tightly coupled to ``apps.sso``'s URL mounting (``/sso/``).
      If you ever re-mount oauth2_provider under a different prefix,
      update both attributes to match. The companion regression tests
      in ``tests/test_login_required_middleware.py`` catch a stale
      allow-list immediately.

    Paths NOT on the allow-list — e.g. ``/sso/authorize/``, which needs
    a session — stay gated (default-deny). django-oauth-toolkit's
    admin-style management views are not mounted at all (see
    ``config/urls.py``); client management lives in ``apps.sso``.

    ``request.path`` carries no locale prefix on the matched URLs
    because the public OIDC endpoints are mounted *outside*
    ``i18n_patterns`` in ``config/urls.py`` (RFC 8414 well-known URLs
    must not carry a locale prefix). Note that Django itself does NOT
    strip locale prefixes from ``request.path`` / ``request.path_info``
    — locale-prefix handling happens at URL-resolution time via
    ``LocalePrefixPattern`` and never writes back to the request
    object. So if a future change ever re-mounted public endpoints
    inside ``i18n_patterns``, every locale-prefixed variant
    (``/de/sso/token/``, ``/en/sso/token/``, …) would have to be added
    to the allow-list — or the matching here would need to be made
    locale-aware. The companion test
    ``test_locale_prefixed_sso_is_gated`` codifies the current contract.
    """

    #: **Exact-match** public paths. Each entry maps 1:1 to a single
    #: view; a hypothetical sub-path (``/sso/token/foo/``) is **not**
    #: covered, so a future view added beneath one of these prefixes
    #: would still be gated by default. That's the intended security
    #: posture — explicit additions only.
    #:
    #: **Invariant:** every entry MUST end with ``/`` to match the
    #: post-normalisation form produced by :pymeth:`_is_public_path`.
    #:
    #: ``/i18n/setlang/`` — Django's language switcher posts here. The
    #: login form itself offers the switcher, so it must be reachable
    #: pre-authentication.
    #:
    #: ``/sso/token/`` — OAuth 2.0 token endpoint (RFC 6749 §3.2). The
    #: client authenticates via the request body, not a Django session.
    #:
    #: ``/sso/revoke_token/`` — OAuth 2.0 token revocation (RFC 7009).
    #: Same auth model as ``/sso/token/``.
    #:
    #: ``/sso/introspect/`` — OAuth 2.0 token introspection (RFC 7662).
    #: Same auth model.
    #:
    #: ``/sso/userinfo/`` — OIDC UserInfo endpoint. Uses Bearer token
    #: authentication, not Django sessions.
    #:
    #: ``/sso/logout/`` — OIDC RP-Initiated Logout. The spec allows
    #: requests without a session to be redirected to the post-logout
    #: URI; the view must run to honour that contract.
    #:
    #: Not on this list: OAuth 2.0 Device Authorization Grant
    #: (RFC 8628) endpoints. ``config/urls.py`` does not mount them at
    #: all (they bypass the per-app SSO policy and no RP uses them).
    PUBLIC_EXACT_PATHS: Final[frozenset[str]] = frozenset(
        {
            "/i18n/setlang/",
            "/sso/token/",
            "/sso/revoke_token/",
            "/sso/introspect/",
            "/sso/userinfo/",
            "/sso/logout/",
        }
    )

    #: **True-prefix** public paths. Anything *beneath* the entry is
    #: covered too — used only for genuine registries whose member URIs
    #: are bounded by an external spec (so the surface doesn't sprawl
    #: silently).
    #:
    #: **Invariant:** every entry MUST end with ``/``. Without the
    #: trailing slash, ``/sso/.well-known`` would also match a
    #: hypothetical ``/sso/.well-knownfoo/`` — boundary-sensitive
    #: matching is the whole point.
    #:
    #: ``/sso/.well-known/`` — RFC 8615 "well-known URIs" registry.
    #: Covers ``openid-configuration`` (OIDC Discovery), ``jwks.json``,
    #: and any future spec-defined ``.well-known`` entry. RPs read
    #: these to bootstrap before any user is involved.
    PUBLIC_PATH_PREFIXES: Final[tuple[str, ...]] = ("/sso/.well-known/",)

    @classmethod
    def _is_public_path(cls, path: str) -> bool:
        """Return True iff ``path`` matches the public allow-list.

        Defense-in-depth: we normalise the path with ``posixpath.normpath``
        before matching so that traversal sequences like
        ``/sso/token/../authorize/`` don't slip through the prefix
        check. Note that Django's URL resolver matches against the raw
        ``PATH_INFO`` and does NOT perform dot-segment normalisation —
        whether such a path resolves to ``/sso/authorize/``, returns
        404, or is rewritten by an upstream reverse proxy depends on
        the deployment. Our normalisation here is therefore the only
        guaranteed defense against an attacker using ``..`` to flip the
        gate decision; we do not rely on Django to re-route the request
        after the fact.

        ``posixpath.normpath`` collapses ``..`` segments and removes
        duplicate inner slashes. Two gotchas to handle:

        * It preserves a leading ``//`` (POSIX allows implementation-defined
          semantics for paths starting with exactly two slashes), so
          ``//sso//token//`` normalises to ``//sso/token``. We collapse
          all leading slashes to a single ``/`` before matching.
        * It drops a trailing ``/``, which would cause an exact-match
          path like ``/sso/token/`` to fail the lookup against
          :pyattr:`PUBLIC_EXACT_PATHS`. We re-append the slash
          afterwards.
        """
        normalised = normalise_path(path)
        if normalised in cls.PUBLIC_EXACT_PATHS:
            return True
        return normalised.startswith(cls.PUBLIC_PATH_PREFIXES)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if self._is_public_path(request.path):
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)


class PrivacyAckMiddleware(MiddlewareMixin):
    """Gate the browser UI until the current privacy policy is acknowledged.

    Active only when ``settings.PRIVACY_POLICY_VERSION > 0`` and the user
    is authenticated with ``privacy_ack_version`` below that version. Such
    a user is redirected to ``accounts:privacy_ack`` (with ``next`` for
    ``GET``/``HEAD``, so e.g. an OIDC ``/sso/authorize/?…`` flow resumes
    exactly where it was). HTMX requests get an ``HX-Redirect`` instead of
    a 302 that would otherwise be swapped into a fragment.

    Only the UI is gated — never the API, the station agent, WebSockets or
    the OIDC protocol endpoints an RP calls server-to-server:

    * Token/DeviceKey auth (DRF) sets ``request.user`` inside the view, so
      at ``process_view`` time those requests are anonymous → untouched.
    * WebSockets don't run through HTTP middleware (Channels).
    * Everything else machine-facing is listed in
      :pyattr:`EXEMPT_PATH_PREFIXES` / the ``/sso/`` rule below.

    ``process_view`` (not ``__call__``) so ``request.resolver_match`` is
    available for :pyattr:`EXEMPT_VIEW_NAMES` regardless of the locale
    prefix; unresolved paths (404) never reach the gate.
    """

    #: Machine-facing path prefixes that are never gated. Matched against
    #: the :func:`normalise_path` form (always ends with ``/``); entries
    #: without a trailing ``/`` (single files like ``/sw.js``) are matched
    #: boundary-sensitively as ``<entry>/`` so ``/sw.jsx`` is not covered.
    #: ``/api/`` is exempt as a whole: session-auth JSON fetches there
    #: cannot digest an HTML redirect either.
    EXEMPT_PATH_PREFIXES: Final[tuple[str, ...]] = (
        "/api/",
        "/static/",
        "/media/",
        "/i18n/",
        "/webpush/",
        "/sw.js",
        "/manifest.webmanifest",
        "/__debug__/",
    )

    #: The OIDC protocol surface under ``/sso/`` is exempt (token, revoke,
    #: introspect, userinfo, RP logout, ``.well-known``/JWKS) — except the
    #: browser authorize flow, which a human walks through and which must
    #: show the notice before handing identity to another app.
    SSO_PREFIX: Final[str] = "/sso/"
    SSO_GATED_PREFIX: Final[str] = "/sso/authorize/"

    #: Views reachable while un-acknowledged (matched on the resolved
    #: ``view_name``, i.e. independent of the locale prefix).
    EXEMPT_VIEW_NAMES: Final[frozenset[str]] = frozenset(
        {"accounts:logout", "accounts:privacy_ack", "set_language"}
    )

    _EXEMPT_PREFIXES_NORMALISED: Final[tuple[str, ...]] = tuple(
        p if p.endswith("/") else p + "/" for p in EXEMPT_PATH_PREFIXES
    )

    @classmethod
    def _is_exempt_path(cls, path: str) -> bool:
        """Return True iff ``path`` is a machine surface (never gated).

        Uses the same :func:`normalise_path` as
        ``LoginRequiredMiddleware._is_public_path`` so ``..`` traversal
        can't turn a gated path into an exempt one.
        """
        normalised = normalise_path(path)
        if normalised.startswith(cls._EXEMPT_PREFIXES_NORMALISED):
            return True
        return normalised.startswith(cls.SSO_PREFIX) and not normalised.startswith(
            cls.SSO_GATED_PREFIX
        )

    def process_view(self, request, view_func, view_args, view_kwargs):
        version = settings.PRIVACY_POLICY_VERSION
        if version <= 0:
            return None
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if getattr(user, "privacy_ack_version", 0) >= version:
            return None
        if self._is_exempt_path(request.path):
            return None
        match = getattr(request, "resolver_match", None)
        if match is not None and match.view_name in self.EXEMPT_VIEW_NAMES:
            return None

        url = reverse("accounts:privacy_ack")
        if request.method in ("GET", "HEAD"):
            url += "?" + urlencode({"next": request.get_full_path()})
        if request.headers.get("HX-Request") == "true":
            return HttpResponseClientRedirect(url)
        return redirect(url)
