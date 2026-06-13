"""Project-wide middleware customisations.

Currently houses ``LoginRequiredMiddleware``, our subclass of Django 5.1+'s
``django.contrib.auth.middleware.LoginRequiredMiddleware`` that bolts a
two-part allow-list (exact paths + true prefixes) onto the framework's
default-deny gate. See the docstring on the class for the rationale.
"""

import posixpath
from typing import Final

from django.contrib.auth.middleware import (
    LoginRequiredMiddleware as DjangoLoginRequiredMiddleware,
)


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

    Paths NOT on the allow-list — including ``/sso/applications/`` and
    ``/sso/authorized_tokens/`` (django-oauth-toolkit's admin-style
    pages) — stay gated, so an anon visitor cannot enumerate registered
    OAuth clients or active tokens.

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
    #: (RFC 8628) endpoints (``device-authorization/``, ``device/``,
    #: ``device-confirm/``, ``device-grant-status/``). The grant is
    #: shipped by ``django-oauth-toolkit`` but is not configured or
    #: used by any current RP. The principle is "no anonymous surface
    #: we don't actively use" — add the relevant entries (with a
    #: companion test) the moment the device flow goes live.
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
        ``/sso/token/../applications/`` don't slip through the prefix
        check. Note that Django's URL resolver matches against the raw
        ``PATH_INFO`` and does NOT perform dot-segment normalisation —
        whether such a path resolves to ``/sso/applications/``, returns
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
        normalised = posixpath.normpath(path)
        # Collapse leading double-slashes that posixpath preserves.
        normalised = "/" + normalised.lstrip("/")
        if not normalised.endswith("/"):
            normalised += "/"
        if normalised in cls.PUBLIC_EXACT_PATHS:
            return True
        return normalised.startswith(cls.PUBLIC_PATH_PREFIXES)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if self._is_public_path(request.path):
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)
