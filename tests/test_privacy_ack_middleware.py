"""Tests for ``config.middleware.PrivacyAckMiddleware``.

The gate redirects an authenticated user who has not yet acknowledged the
current privacy-policy version to the acknowledgement page — but only for
the browser UI. Machine surfaces (API, OIDC protocol endpoints, service
worker, manifest, i18n switch) must never be redirected.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings as dj_settings
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from config.middleware import PrivacyAckMiddleware


@pytest.fixture
def gated(settings, client, member_user):
    """Gate on (version 1), member logged in, not acknowledged."""
    settings.PRIVACY_POLICY_VERSION = 1
    client.force_login(member_user)
    return client


def _ack_url():
    return reverse("accounts:privacy_ack")


def _next_of(url):
    return parse_qs(urlsplit(url).query).get("next")


def test_middleware_registered_after_login_required():
    mw = dj_settings.MIDDLEWARE
    login_idx = mw.index("config.middleware.LoginRequiredMiddleware")
    assert mw.index("config.middleware.PrivacyAckMiddleware") == login_idx + 1


@pytest.mark.django_db
def test_unacked_user_redirected_with_next(gated):
    target = reverse("dashboard:index")
    r = gated.get(target)
    assert r.status_code == 302
    assert urlsplit(r.url).path == _ack_url()
    assert _next_of(r.url) == [target]


@pytest.mark.django_db
def test_acked_user_passes(gated, member_user):
    member_user.privacy_ack_version = 1
    member_user.save(update_fields=["privacy_ack_version"])
    r = gated.get(reverse("dashboard:index"))
    assert r.status_code == 200


@pytest.mark.django_db
def test_old_version_ack_redirected(gated, member_user, settings):
    settings.PRIVACY_POLICY_VERSION = 2
    member_user.privacy_ack_version = 1
    member_user.save(update_fields=["privacy_ack_version"])
    r = gated.get(reverse("dashboard:index"))
    assert r.status_code == 302
    assert urlsplit(r.url).path == _ack_url()


@pytest.mark.django_db
def test_version_zero_disables_gate(gated, settings):
    settings.PRIVACY_POLICY_VERSION = 0
    r = gated.get(reverse("dashboard:index"))
    assert r.status_code == 200


@pytest.mark.django_db
def test_anonymous_untouched(client, settings):
    settings.PRIVACY_POLICY_VERSION = 1
    r = client.get(reverse("dashboard:index"))
    assert r.status_code == 302
    assert urlsplit(r.url).path == reverse("accounts:login")
    assert "privacy" not in r.url


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/health/",
        "/sso/userinfo/",
        "/sso/token/",
        "/sso/.well-known/openid-configuration/",
        "/sw.js",
        "/manifest.webmanifest",
        "/i18n/setlang/",
    ],
)
def test_machine_paths_exempt(gated, path):
    r = gated.get(path)
    assert "privacy" not in (r.get("Location") or "")
    assert "HX-Redirect" not in r


@pytest.mark.django_db
def test_logout_exempt(gated):
    r = gated.post(reverse("accounts:logout"))
    assert "privacy" not in (r.get("Location") or "")


@pytest.mark.django_db
def test_ack_page_itself_not_looped(gated):
    r = gated.get(_ack_url())
    assert r.status_code == 200


@pytest.mark.django_db
def test_sso_authorize_is_gated_and_keeps_query(gated):
    r = gated.get("/sso/authorize/?client_id=abc&state=xyz")
    assert r.status_code == 302
    assert urlsplit(r.url).path == _ack_url()
    assert _next_of(r.url) == ["/sso/authorize/?client_id=abc&state=xyz"]


@pytest.mark.django_db
def test_htmx_request_gets_hx_redirect(gated):
    r = gated.get(reverse("dashboard:index"), HTTP_HX_REQUEST="true")
    assert r.status_code == 200
    assert _ack_url() in r["HX-Redirect"]


@pytest.mark.django_db
def test_htmx_redirect_returns_to_current_page_not_fragment(gated):
    """A polling partial must not become the post-ack landing page."""
    r = gated.get(
        reverse("dashboard:index"),
        HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL="https://testserver/en/stations/7/?tab=audit",
    )
    assert _next_of(r["HX-Redirect"]) == ["/en/stations/7/?tab=audit"]


@pytest.mark.django_db
def test_htmx_redirect_without_current_url_has_no_next(gated):
    r = gated.get(reverse("dashboard:index"), HTTP_HX_REQUEST="true")
    assert _next_of(r["HX-Redirect"]) is None


@pytest.mark.django_db
def test_htmx_post_returns_to_current_page(gated):
    r = gated.post(
        reverse("dashboard:index"),
        HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL="https://testserver/en/",
    )
    assert _next_of(r["HX-Redirect"]) == ["/en/"]


@pytest.mark.django_db
def test_post_redirect_has_no_next(gated):
    r = gated.post(reverse("dashboard:index"))
    assert r.status_code == 302
    assert urlsplit(r.url).path == _ack_url()
    assert _next_of(r.url) is None


@pytest.mark.parametrize(
    "path",
    ["/api/../en/", "/sso/token/../authorize/", "//api/../en/", "/sw.js/../en/"],
)
def test_traversal_is_not_exempt(path):
    assert PrivacyAckMiddleware._is_exempt_path(path) is False


@pytest.mark.parametrize(
    "path",
    ["/sso/authorize/", "/sso/authorize", "/en/", "/sw.jsx", "/apix/", "/en/api/"],
)
def test_ui_paths_not_exempt(path):
    assert PrivacyAckMiddleware._is_exempt_path(path) is False


@pytest.mark.parametrize(
    "path",
    ["/api/v1/health/", "/sso/token/", "/sso/logout/", "/sw.js", "/manifest.webmanifest"],
)
def test_machine_paths_are_exempt_unit(path):
    assert PrivacyAckMiddleware._is_exempt_path(path) is True


@pytest.mark.django_db
def test_traversal_does_not_bypass(rf: RequestFactory, member_user, settings):
    """A traversal path that *would* be exempt by naive prefix matching is gated."""
    settings.PRIVACY_POLICY_VERSION = 1
    mw = PrivacyAckMiddleware(lambda request: HttpResponse("ok"))
    for path in ("/api/../en/", "/sso/token/../authorize/"):
        request = rf.get(path)
        request.user = member_user
        request.resolver_match = None
        response = mw.process_view(request, lambda r: HttpResponse("ok"), (), {})
        assert response is not None and response.status_code == 302
        assert urlsplit(response.url).path == _ack_url()
