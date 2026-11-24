"""JS translation catalog (spec 2026-11-24 i18n-de, section "JS").

``/<lang>/jsi18n/`` serves Django's JavaScriptCatalog: anonymous, never
gated by the privacy notice (it is a script — an HTML redirect would break
every page), and included CSP-conformly before the app scripts.
"""

import re

import pytest
from django.urls import reverse
from django.utils import translation


def _catalog_url(lang):
    with translation.override(lang):
        return reverse("javascript-catalog")


def test_catalog_url_is_locale_prefixed():
    assert _catalog_url("en") == "/en/jsi18n/"
    assert _catalog_url("de") == "/de/jsi18n/"


@pytest.mark.django_db
@pytest.mark.parametrize("lang", ["en", "de"])
def test_catalog_served_anonymously_as_javascript(client, lang):
    resp = client.get(_catalog_url(lang))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/javascript")
    body = resp.content.decode()
    assert "django.gettext" in body
    assert "interpolate" in body


@pytest.mark.django_db
def test_catalog_browser_cache_is_short_and_revalidatable(client):
    # The URL is unversioned: a long browser max-age would replay stale
    # translations after a deploy. Browsers get 5 min + Last-Modified.
    resp = client.get(_catalog_url("de"))
    assert "max-age=300" in resp.get("Cache-Control", "")
    assert "max-age=86400" not in resp.get("Cache-Control", "")
    assert resp.has_header("Last-Modified")


@pytest.mark.django_db
def test_catalog_conditional_get_returns_304(client):
    first = client.get(_catalog_url("de"))
    resp = client.get(_catalog_url("de"), HTTP_IF_MODIFIED_SINCE=first["Last-Modified"])
    assert resp.status_code == 304


@pytest.mark.django_db
def test_catalog_not_gated_by_privacy_notice(client, member_user, settings):
    settings.PRIVACY_POLICY_VERSION = 99
    member_user.privacy_ack_version = 0
    member_user.save(update_fields=["privacy_ack_version"])
    client.force_login(member_user)
    resp = client.get(_catalog_url("de"))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/javascript")


def _script_tag_re(src):
    return re.compile(r'<script src="' + re.escape(src) + r'" nonce="[^"]+"></script>')


@pytest.mark.django_db
def test_base_html_includes_catalog_with_nonce_before_app_js(client, member_user, settings):
    client.force_login(member_user)
    # The cookie pins the page language (else UserLanguageMiddleware would
    # redirect to the user's own language first).
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "de"
    with translation.override("de"):
        url = reverse("dashboard:index")
    resp = client.get(url)
    assert resp.status_code == 200
    body = resp.content.decode()
    m = _script_tag_re("/de/jsi18n/").search(body)
    assert m, "catalog <script> with nonce missing in base.html"
    assert m.start() < body.index("js/app."), "catalog must load before app.js"


@pytest.mark.django_db
def test_login_page_includes_catalog_with_nonce_before_app_js(client):
    with translation.override("en"):
        url = reverse("accounts:login")
    body = client.get(url).content.decode()
    m = _script_tag_re("/en/jsi18n/").search(body)
    assert m, "catalog <script> with nonce missing on the standalone login page"
    assert m.start() < body.index("js/app.")
