"""Tests for the global no-anonymous-access enforcement.

Issue #73: Wire Django 5.1+'s ``LoginRequiredMiddleware`` into MIDDLEWARE
so that every view is login-required by default. A handful of endpoints
(health check, station-agent heartbeat & deployment API, OAuth/OIDC
public surface, login form, language switcher) must remain reachable
without a session — those are exercised here as regression tests.

The middleware is **belt-and-suspenders** with the existing per-view
``LoginRequiredMixin`` family. The mixins stay; the middleware closes
the gap a future view-author can leave open by forgetting them.
"""

from importlib import import_module

import pytest
from django.conf import settings
from django.test import RequestFactory
from django.urls import reverse

# ---------------------------------------------------------------------------
# Settings — the middleware must be registered, and after AuthenticationMW
# ---------------------------------------------------------------------------


def _middleware_index(suffix: str) -> int:
    for i, mw in enumerate(settings.MIDDLEWARE):
        if mw.endswith(suffix):
            return i
    raise AssertionError(
        f"No middleware ending with {suffix!r} found in MIDDLEWARE: {settings.MIDDLEWARE}"
    )


def test_login_required_middleware_is_registered():
    """A subclass or vanilla ``LoginRequiredMiddleware`` must appear in
    ``settings.MIDDLEWARE`` — otherwise the policy collapses to the
    per-view mixins alone, which is exactly what this issue fixes."""
    _middleware_index("LoginRequiredMiddleware")


def test_login_required_middleware_runs_after_authentication_middleware():
    """LoginRequiredMiddleware reads ``request.user``; the user is set up
    by ``AuthenticationMiddleware``. Reversed order means the middleware
    sees an unauthenticated request even for logged-in users."""
    auth_idx = _middleware_index("AuthenticationMiddleware")
    login_idx = _middleware_index("LoginRequiredMiddleware")
    assert auth_idx < login_idx, (
        "LoginRequiredMiddleware must come AFTER AuthenticationMiddleware "
        f"in MIDDLEWARE (auth at {auth_idx}, login at {login_idx})."
    )


# ---------------------------------------------------------------------------
# Middleware unit test — synthetic naked view proves the gate fires
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_middleware_redirects_a_naked_anonymous_view():
    """The whole point of the middleware: a *future* view that forgets
    ``LoginRequiredMixin`` must still be inaccessible to anon users.

    Hard to demonstrate against the real URL conf — every existing view
    already has its mixin. Instead, we feed a synthetic view function
    (no ``login_required = False``, no mixin) through the middleware
    directly and assert it produces a redirect for an anon request.
    """
    from django.contrib.auth.models import AnonymousUser

    # Find the registered LoginRequiredMiddleware so we test exactly the
    # middleware in MIDDLEWARE (subclass or vanilla — both are valid).
    dotted = next(mw for mw in settings.MIDDLEWARE if mw.endswith("LoginRequiredMiddleware"))
    module_path, cls_name = dotted.rsplit(".", 1)
    mw_cls = getattr(import_module(module_path), cls_name)
    mw = mw_cls(get_response=lambda r: None)

    def naked_view(request):  # pragma: no cover - never called by middleware
        raise AssertionError("naked view should not be reached")

    rf = RequestFactory()
    request = rf.get("/anything/")
    request.user = AnonymousUser()

    response = mw.process_view(request, naked_view, (), {})
    assert response is not None, "Middleware let an anonymous request through"
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


# ---------------------------------------------------------------------------
# Acceptance-criteria integration tests (issue #73)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_redirects_anonymous_user_to_login(client):
    """Per acceptance criteria: ``GET /dashboard/`` (here: ``GET /``,
    routed to ``dashboard:index``) as anon returns 302 → login with the
    intended ``next`` query parameter."""
    target = reverse("dashboard:index")
    response = client.get(target)
    assert response.status_code == 302
    # Locale prefix may sit in front of /accounts/login/, so we substring-check.
    assert "/accounts/login/" in response.url
    assert "next=" in response.url


@pytest.mark.django_db
def test_health_endpoint_anon_accessible(client):
    """Acceptance criterion: ``GET /api/v1/health/`` as anon returns 200.
    Uptime monitors hit this endpoint without credentials."""
    response = client.get(reverse("api:health"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_oidc_discovery_anon_accessible(client):
    """Acceptance criterion: ``GET /sso/.well-known/openid-configuration``
    as anon returns 200. RP-side bootstrap (e.g. InvenTree) reads this
    before any user is involved — it must not be gated."""
    response = client.get("/sso/.well-known/openid-configuration/")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Exempt-surface regression tests (must keep working after middleware lands)
# ---------------------------------------------------------------------------


def _is_login_redirect(response) -> bool:
    """True iff the response is a 302 to the login page (regardless of locale
    prefix). We use this to distinguish a session-gate redirect from any
    other 302 a view might legitimately issue (e.g. ``set_language``)."""
    return response.status_code == 302 and "/accounts/login/" in (response.url or "")


@pytest.mark.django_db
def test_oidc_jwks_anon_accessible(client):
    """RPs fetch JWKS to verify ID-Token signatures — must be reachable
    without a Django session."""
    response = client.get("/sso/.well-known/jwks.json")
    assert response.status_code == 200


@pytest.mark.django_db
def test_oauth_token_endpoint_does_not_redirect_anon(client):
    """OAuth token exchange happens server-to-server with no Django
    session. The middleware MUST NOT 302 anon requests here; the
    downstream view validates the body and returns 400 on its own.

    We assert both the negative ("not redirected to login") AND the
    positive (400/401 from oauth2_provider's own validation) to keep
    this test from silently passing on a 500 or 200."""
    response = client.post("/sso/token/", {})
    assert not _is_login_redirect(response), (
        "Token endpoint was redirected to login by the middleware; "
        "OAuth clients have no session and will silently break."
    )
    assert response.status_code in (400, 401), (
        f"Token endpoint returned {response.status_code}; expected a 4xx "
        "from oauth2_provider's own validation of the empty body."
    )


@pytest.mark.django_db
def test_oauth_revoke_token_endpoint_does_not_redirect_anon(client):
    """OAuth revoke endpoint — same logic as token: clients are
    server-to-server, no Django session."""
    response = client.post("/sso/revoke_token/", {})
    assert not _is_login_redirect(response)
    assert response.status_code in (400, 401, 200), response.status_code


@pytest.mark.django_db
def test_oauth_introspect_endpoint_does_not_redirect_anon(client):
    """Token introspection — RFC 7662. Anonymous clients are valid
    (they authenticate by including client credentials in the body).

    oauth2_provider's view returns 403 when neither bearer token nor
    client credentials are supplied — that's the view doing its OWN
    permission check after our middleware let the request through. The
    pass condition is the absence of a session redirect, not a specific
    4xx code."""
    response = client.post("/sso/introspect/", {})
    assert not _is_login_redirect(response)
    assert response.status_code in (400, 401, 403), response.status_code


@pytest.mark.django_db
def test_oidc_userinfo_endpoint_does_not_redirect_anon(client):
    """UserInfo uses Bearer authentication, not Django sessions.
    Anon → 401 from OAuth, not a 302 to the login form."""
    response = client.get("/sso/userinfo/")
    assert not _is_login_redirect(response)
    assert response.status_code == 401, response.status_code


@pytest.mark.django_db
def test_sso_authorize_redirects_anonymous_to_login(client):
    """``/sso/authorize/`` is the OAuth authorization endpoint — it's
    user-facing (the consent screen) and MUST require a session. The
    issue's exemption table explicitly excludes it; this test guards
    against an accidental addition to ``PUBLIC_PATH_PREFIXES`` that
    would expose the consent flow to anonymous redirection."""
    response = client.get("/sso/authorize/")
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


@pytest.mark.django_db
def test_admin_login_anon_accessible(client):
    """Django admin's own login form must be reachable without a
    session. Django 5.1+ ships it with ``@login_not_required`` upstream,
    so this is a regression test against an upstream change or a local
    override that breaks the admin bootstrap.

    ``/admin/login/`` is mounted inside ``i18n_patterns`` (see
    ``config/urls.py``), so the first request gets a 302 to a
    locale-prefixed URL (``/en/admin/login/``). We follow it and assert
    the final response is 200, NOT another redirect to ``/accounts/login/``."""
    response = client.get("/admin/login/", follow=True)
    assert response.status_code == 200
    # redirect_chain is a list of (url, status_code) tuples.
    assert not any("/accounts/login/" in url for url, _ in response.redirect_chain), (
        f"admin login was redirected to /accounts/login/: {response.redirect_chain!r}"
    )


@pytest.mark.django_db
def test_sso_applications_redirects_anonymous(client):
    """oauth2_provider's admin-style endpoints (``/sso/applications/``,
    ``/sso/authorized_tokens/``) must STAY gated — they aren't on the
    public allow-list and would otherwise expose the OAuth client
    registry."""
    response = client.get("/sso/applications/")
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


@pytest.mark.django_db
def test_heartbeat_endpoint_does_not_redirect_anon(client):
    """Station-agent heartbeats authenticate via ``DeviceKeyAuthentication``.
    A request without the device-key headers must be rejected by DRF (401),
    not 302'd to a login form the agent can't follow."""
    response = client.post(reverse("api:heartbeat"), data={}, content_type="application/json")
    assert not _is_login_redirect(response)
    # DRF returns 401 for unauthenticated requests when the only auth
    # class (DeviceKeyAuthentication) rejects.
    assert response.status_code == 401


@pytest.mark.django_db
def test_deployment_check_endpoint_does_not_redirect_anon(client):
    """``/api/v1/deployments/check/`` is the station agent's poll endpoint.
    Same auth model as heartbeat: DeviceKey, never a Django session."""
    response = client.post("/api/v1/deployments/check/", data={}, content_type="application/json")
    assert not _is_login_redirect(response)


@pytest.mark.django_db
def test_deployment_commit_endpoint_does_not_redirect_anon(client):
    response = client.post("/api/v1/deployments/commit/", data={}, content_type="application/json")
    assert not _is_login_redirect(response)


@pytest.mark.django_db
def test_deployment_status_update_endpoint_does_not_redirect_anon(client):
    response = client.post(
        "/api/v1/deployments/1/status/", data={}, content_type="application/json"
    )
    assert not _is_login_redirect(response)


@pytest.mark.django_db
def test_deployment_download_endpoint_does_not_redirect_anon(client):
    response = client.get("/api/v1/deployments/1/download/")
    assert not _is_login_redirect(response)


@pytest.mark.django_db
def test_login_page_anon_accessible(client):
    """Cannot log in if the login page itself is gated."""
    response = client.get(reverse("accounts:login"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_setlang_anon_accessible(client):
    """The language switcher posts to ``/i18n/setlang/`` from pre-login
    pages (login form, error pages). It must not be gated.

    ``set_language`` returns 302 to the referer/next — we assert the
    redirect target is NOT the login page."""
    response = client.post(
        "/i18n/setlang/",
        {"language": "en", "next": "/"},
    )
    assert not _is_login_redirect(response)


# ---------------------------------------------------------------------------
# Path-prefix attack vectors — defense-in-depth regression tests
# ---------------------------------------------------------------------------
#
# ``request.path`` is the raw URL path Django sees pre-resolution. Naïve
# ``startswith`` matching against an allow-list is a known footgun:
# traversal sequences (``..``), double-slashes, and locale prefixes can
# all produce surprising matches. The middleware normalises with
# ``posixpath.normpath`` before checking; these tests pin that contract.


@pytest.mark.django_db
def test_path_traversal_does_not_bypass_gate(client):
    """``/sso/.well-known/../applications/`` literally starts with
    ``/sso/.well-known/`` so a raw ``startswith`` check would let it
    through. Normalised, it resolves to ``/sso/applications/`` which
    must stay gated."""
    response = client.get("/sso/.well-known/../applications/")
    # Django's CommonMiddleware will normalise and redirect (301/302) or
    # the resolver routes the normalised path; either way the visible
    # outcome from an anonymous request must NOT be 200 (would imply
    # we bypassed the gate to the applications view).
    assert response.status_code != 200
    # If it redirects, it should be either a normalisation 301 or a
    # login 302 — never a token/.well-known view's 2xx.


@pytest.mark.django_db
def test_locale_prefixed_sso_is_gated(client):
    """OIDC public endpoints are mounted OUTSIDE ``i18n_patterns`` so
    they never carry a locale prefix. ``/de/sso/token/`` is therefore
    NOT the token endpoint — it's an unrelated URL that should not
    inherit the allow-list. Confirming gated keeps a sloppy reverse
    proxy or a future i18n re-mounting from accidentally opening a hole."""
    response = client.get("/de/sso/token/")
    # Either gated by middleware (302 to login) or simply 404 — both are
    # fine outcomes; the only outcome we want to prevent is an anon 200.
    assert response.status_code != 200


@pytest.mark.django_db
def test_double_slash_does_not_change_gate_decision(client):
    """``//sso//token//`` collapses to ``/sso/token/`` after
    normalisation. The middleware must still apply the allow-list to
    the normalised form, not the literal one."""
    response = client.post("//sso//token//", {})
    # Token endpoint with empty body → 400 from oauth2_provider. The
    # key assertion is "not a login redirect" — we got past the gate.
    assert not _is_login_redirect(response)


def test_normalisation_helper_blocks_traversal_to_gated_path():
    """Unit test for the ``_is_public_path`` helper. Traversal that
    *resolves* to a non-public path must return False even though the
    raw string starts with a public prefix."""
    from config.middleware import LoginRequiredMiddleware as Mw

    assert Mw._is_public_path("/sso/token/") is True
    assert Mw._is_public_path("/sso/.well-known/openid-configuration/") is True
    assert Mw._is_public_path("/sso/applications/") is False
    assert Mw._is_public_path("/sso/authorize/") is False
    # Traversal: literal prefix matches but normalisation resolves
    # to a gated path → must return False.
    assert Mw._is_public_path("/sso/token/../applications/") is False
    assert Mw._is_public_path("/sso/.well-known/../applications/") is False
    # Double slashes collapse but resolve to the same public path → True.
    assert Mw._is_public_path("//sso//token//") is True
