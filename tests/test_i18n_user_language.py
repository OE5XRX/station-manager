"""UI language follows ``User.language`` (spec 2026-11-24-i18n-de, "Sprache ↔ User").

No German catalogue is asserted here — only behaviour: the language cookie,
the locale prefix of redirects and the persisted ``User.language``.
"""

from urllib.parse import urlsplit

import pytest
from django.conf import settings as dj_settings
from django.urls import reverse
from django.utils import translation

from apps.accounts.language import localized_url, set_language_cookie
from config.middleware import UserLanguageMiddleware

COOKIE = dj_settings.LANGUAGE_COOKIE_NAME
PASSWORD = "testpass123"


@pytest.fixture
def de_user(member_user):
    member_user.language = "de"
    member_user.save(update_fields=["language"])
    return member_user


def _login_url(lang="en"):
    with translation.override(lang):
        return reverse("accounts:login")


def _profile_url(lang):
    with translation.override(lang):
        return reverse("accounts:profile")


def _dashboard_url(lang):
    with translation.override(lang):
        return reverse("dashboard:index")


# --- helper ------------------------------------------------------------------


def test_localized_url_switches_prefix_and_keeps_query():
    assert localized_url("/en/stations/?a=1", "de") == "/de/stations/?a=1"
    assert localized_url("/de/stations/", "en") == "/en/stations/"


def test_localized_url_leaves_unprefixed_and_unknown_alone():
    url = "/sso/authorize/?client_id=x&next=/en/"
    assert localized_url(url, "de") == url
    assert localized_url("/en/stations/", "xx") == "/en/stations/"
    assert localized_url("/en/does-not-exist/", "de") == "/en/does-not-exist/"


def test_set_language_cookie_uses_settings_flags(settings):
    from django.http import HttpResponse

    settings.LANGUAGE_COOKIE_SECURE = True
    r = HttpResponse()
    assert set_language_cookie(r, "de") is True
    c = r.cookies[COOKIE]
    assert c.value == "de"
    assert c["httponly"] is True
    assert c["secure"] is True
    assert c["samesite"] == "Lax"
    assert int(c["max-age"]) == dj_settings.LANGUAGE_COOKIE_AGE
    r2 = HttpResponse()
    assert set_language_cookie(r2, "xx") is False
    assert COOKIE not in r2.cookies


# --- login -------------------------------------------------------------------


@pytest.mark.django_db
def test_login_redirects_to_user_language_and_sets_cookie(client, de_user):
    r = client.post(_login_url("en"), {"username": "member", "password": PASSWORD})
    assert r.status_code == 302
    assert r["Location"] == _dashboard_url("de")
    assert r["Location"].startswith("/de/")
    assert r.cookies[COOKIE].value == "de"


@pytest.mark.django_db
def test_login_next_prefixed_is_translated(client, de_user):
    next_url = "/en/stations/?x=1"
    r = client.post(
        _login_url("en") + "?next=" + next_url,
        {"username": "member", "password": PASSWORD, "next": next_url},
    )
    assert r.status_code == 302
    assert r["Location"] == "/de/stations/?x=1"


@pytest.mark.django_db
def test_login_next_sso_authorize_stays_unprefixed(client, de_user):
    next_url = "/sso/authorize/?client_id=abc&response_type=code"
    r = client.post(
        _login_url("en"),
        {"username": "member", "password": PASSWORD, "next": next_url},
    )
    assert r.status_code == 302
    assert r["Location"] == next_url
    assert r.cookies[COOKIE].value == "de"


@pytest.mark.django_db
def test_login_english_user_from_de_page_goes_to_en(client, member_user):
    r = client.post(_login_url("de"), {"username": "member", "password": PASSWORD})
    assert r.status_code == 302
    assert r["Location"].startswith("/en/")
    assert r.cookies[COOKIE].value == "en"


@pytest.mark.django_db
def test_failed_login_sets_no_cookie(client, de_user):
    r = client.post(_login_url("en"), {"username": "member", "password": "wrong"})
    assert r.status_code == 200
    assert COOKIE not in r.cookies


# --- switcher ----------------------------------------------------------------


def test_switcher_url_is_ours_at_the_same_path():
    from django.urls import resolve

    from apps.accounts import views

    assert reverse("set_language") == "/i18n/setlang/"
    assert resolve("/i18n/setlang/").func is views.set_language


@pytest.mark.django_db
def test_switcher_logged_in_persists_language(client, member_user):
    client.force_login(member_user)
    client.cookies[COOKIE] = "en"
    r = client.post("/i18n/setlang/", {"language": "de", "next": "/en/stations/"})
    assert r.status_code == 302
    assert r["Location"] == "/de/stations/"
    assert r.cookies[COOKIE].value == "de"
    member_user.refresh_from_db()
    assert member_user.language == "de"


@pytest.mark.django_db
def test_switcher_logged_in_without_cookie_keeps_new_language(client, member_user):
    """The middleware must not overwrite the switcher's fresh cookie."""
    client.force_login(member_user)
    r = client.post("/i18n/setlang/", {"language": "de", "next": "/en/"})
    assert r.cookies[COOKIE].value == "de"
    member_user.refresh_from_db()
    assert member_user.language == "de"


@pytest.mark.django_db
def test_switcher_invalid_language_does_not_persist(client, member_user):
    client.force_login(member_user)
    client.cookies[COOKIE] = "en"
    r = client.post("/i18n/setlang/", {"language": "xx", "next": "/en/"})
    assert r.status_code == 302
    assert COOKIE not in r.cookies
    member_user.refresh_from_db()
    assert member_user.language == "en"


@pytest.mark.django_db
def test_switcher_get_does_not_persist(client, member_user):
    client.force_login(member_user)
    client.cookies[COOKIE] = "en"
    client.get("/i18n/setlang/", {"language": "de", "next": "/en/"})
    member_user.refresh_from_db()
    assert member_user.language == "en"


@pytest.mark.django_db
def test_switcher_anonymous_only_sets_cookie(client, django_user_model):
    before = list(django_user_model.objects.values_list("pk", "language"))
    r = client.post("/i18n/setlang/", {"language": "de", "next": _login_url("en")})
    assert r.status_code == 302
    assert r["Location"] == _login_url("de")
    assert r.cookies[COOKIE].value == "de"
    assert list(django_user_model.objects.values_list("pk", "language")) == before


# --- profile -----------------------------------------------------------------


def _identity_post(user, **overrides):
    data = {
        "form_name": "identity",
        "identity-email": user.email or "member@example.org",
        "identity-first_name": user.first_name,
        "identity-last_name": user.last_name,
        "identity-language": user.language,
    }
    data.update({f"identity-{k}": v for k, v in overrides.items()})
    return data


@pytest.mark.django_db
def test_profile_language_change_redirects_to_new_prefix(client, member_user):
    member_user.email = "member@example.org"
    member_user.save(update_fields=["email"])
    client.force_login(member_user)
    client.cookies[COOKIE] = "en"
    r = client.post(_profile_url("en"), _identity_post(member_user, language="de"))
    assert r.status_code == 302
    assert r["Location"] == _profile_url("de")
    assert r.cookies[COOKIE].value == "de"
    member_user.refresh_from_db()
    assert member_user.language == "de"


@pytest.mark.django_db
def test_profile_other_change_keeps_prefix_and_cookie(client, member_user):
    member_user.email = "member@example.org"
    member_user.save(update_fields=["email"])
    client.force_login(member_user)
    client.cookies[COOKIE] = "en"
    r = client.post(_profile_url("en"), _identity_post(member_user, first_name="Max"))
    assert r.status_code == 302
    assert r["Location"] == _profile_url("en")
    assert COOKIE not in r.cookies
    member_user.refresh_from_db()
    assert member_user.first_name == "Max"


# --- middleware --------------------------------------------------------------


def test_middleware_registered_after_authentication():
    mw = dj_settings.MIDDLEWARE
    auth_idx = mw.index("django.contrib.auth.middleware.AuthenticationMiddleware")
    assert mw.index("config.middleware.UserLanguageMiddleware") == auth_idx + 1


@pytest.mark.django_db
def test_middleware_session_without_cookie_redirects_once(client, de_user):
    client.force_login(de_user)
    r = client.get("/en/stations/?foo=bar")
    assert r.status_code == 302
    assert r["Location"] == "/de/stations/?foo=bar"
    assert r.cookies[COOKIE].value == "de"
    # The test client keeps the cookie → the follow-up is not redirected again.
    r2 = client.get(r["Location"])
    assert r2.status_code == 200
    # An explicit /en/ link is respected once the cookie exists.
    r3 = client.get("/en/stations/")
    assert r3.status_code == 200
    assert COOKIE not in r3.cookies


@pytest.mark.django_db
def test_middleware_matching_prefix_only_sets_cookie(client, de_user):
    client.force_login(de_user)
    r = client.get("/de/stations/")
    assert r.status_code == 200
    assert r.cookies[COOKIE].value == "de"


@pytest.mark.django_db
def test_middleware_with_cookie_does_nothing(client, de_user):
    client.force_login(de_user)
    client.cookies[COOKIE] = "en"
    r = client.get("/en/stations/")
    assert r.status_code == 200
    assert COOKIE not in r.cookies


@pytest.mark.django_db
def test_middleware_htmx_never_redirects(client, de_user):
    client.force_login(de_user)
    r = client.get("/en/stations/", HTTP_HX_REQUEST="true")
    assert r.status_code == 200
    assert r.cookies[COOKIE].value == "de"


@pytest.mark.django_db
def test_middleware_post_never_redirects(client, de_user):
    de_user.email = "member@example.org"
    de_user.save(update_fields=["email"])
    client.force_login(de_user)
    r = client.post(_profile_url("en"), _identity_post(de_user, first_name="Max"))
    assert r.status_code == 302
    assert urlsplit(r["Location"]).path == _profile_url("en")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    ["/api/v1/stations/", "/sso/userinfo/", "/i18n/setlang/", "/de/jsi18n/", "/sw.js"],
)
def test_middleware_machine_paths_untouched(rf, de_user, path):
    from django.http import HttpResponse

    request = rf.get(path)
    request.user = de_user
    mw = UserLanguageMiddleware(lambda req: HttpResponse("ok"))
    r = mw(request)
    assert r.status_code == 200
    assert COOKIE not in r.cookies


@pytest.mark.django_db
def test_middleware_api_never_redirects(client, de_user):
    client.force_login(de_user)
    r = client.get("/api/v1/stations/")
    assert r.status_code != 302
    assert COOKIE not in r.cookies


@pytest.mark.django_db
def test_middleware_anonymous_untouched(client):
    r = client.get(_login_url("en"))
    assert r.status_code == 200
    assert COOKIE not in r.cookies
