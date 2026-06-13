"""Project-wide middleware customisations.

Currently houses ``LoginRequiredMiddleware``, our subclass of Django 5.1+'s
``django.contrib.auth.middleware.LoginRequiredMiddleware`` that bolts a
path-prefix allow-list onto the framework's default-deny gate. See the
docstring on the class for the rationale.
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

    Instead we maintain a path-prefix allow-list here. The trade-offs:

    * Pro: a *single* place in the code base declares which OIDC/OAuth
      paths are public. Adding a new public endpoint is one line.
    * Pro: the third-party views need no patching; an upstream
      django-oauth-toolkit refactor can land without touching us.
    * Con: tightly coupled to ``apps.sso``'s URL mounting (``/sso/``).
      If you ever re-mount oauth2_provider under a different prefix,
      update :pyattr:`PUBLIC_PATH_PREFIXES` to match. The companion
      regression tests in ``tests/test_login_required_middleware.py``
      catch a stale allow-list immediately.

    Paths NOT on the allow-list — including ``/sso/applications/`` and
    ``/sso/authorized_tokens/`` (django-oauth-toolkit's admin-style
    pages) — stay gated, so an anon visitor cannot enumerate registered
    OAuth clients or active tokens.

    ``request.path`` is the post-locale-strip path because the public
    OIDC endpoints are mounted *outside* ``i18n_patterns`` in
    ``config/urls.py`` (RFC 8414 well-known URLs must not carry a locale
    prefix). Sites that move public endpoints inside i18n_patterns will
    need locale-aware matching here.
    """

    #: Path prefixes that bypass the login gate.
    #:
    #: **Invariant:** every entry MUST end with ``/``. Without the
    #: trailing slash, ``/sso/token`` would also match a hypothetical
    #: ``/sso/tokenfoo/`` — boundary-sensitive prefix matching is the
    #: whole point of the allow-list.
    #:
    #: ``/i18n/setlang/`` — Django's language switcher posts here. The
    #: login form itself offers the switcher, so it must be reachable
    #: pre-authentication.
    #:
    #: ``/sso/.well-known/`` — OIDC Discovery & JWKS endpoints; RPs
    #: read these to bootstrap before any user is involved.
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
    PUBLIC_PATH_PREFIXES: Final[tuple[str, ...]] = (
        "/i18n/setlang/",
        "/sso/.well-known/",
        "/sso/token/",
        "/sso/revoke_token/",
        "/sso/introspect/",
        "/sso/userinfo/",
        "/sso/logout/",
    )

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
          path like ``/sso/token/`` to fail the
          ``startswith('/sso/token/')`` check. We re-append the slash
          afterwards.
        """
        normalised = posixpath.normpath(path)
        # Collapse leading double-slashes that posixpath preserves.
        normalised = "/" + normalised.lstrip("/")
        if not normalised.endswith("/"):
            normalised += "/"
        return normalised.startswith(cls.PUBLIC_PATH_PREFIXES)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if self._is_public_path(request.path):
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)
