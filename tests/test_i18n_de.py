"""German UI renders German (spec 2026-11-24-i18n-de, Task 7).

Each page check pins a few msgids and their German msgstr from
``locale/de/LC_MESSAGES/django.po``: under ``/de/`` the German text must
appear and the English source must not; under ``/en/`` it is the other way
round. A smoke test renders a broad set of UI pages under ``/de/`` so a
broken ``{% blocktrans %}`` / format string in the German catalogue surfaces
as a 500 here, not in production.
"""

import pytest
from django.conf import settings as dj_settings
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import translation
from django.utils.html import escape

COOKIE = dj_settings.LANGUAGE_COOKIE_NAME

# (url name, [(English msgid, German msgstr), ...])
PAGES = {
    "dashboard": (
        "dashboard:index",
        [("New station", "Neue Station"), ("New deployment", "Neues Deployment")],
    ),
    "profile": (
        "accounts:profile",
        [
            ("Save identity", "Identität speichern"),
            (
                "Manage your identity, profile, contact details and location.",
                "Verwalte deine Identität, Profil, Kontaktdaten und Standort.",
            ),
        ],
    ),
    "station_list": (
        "stations:station_list",
        [
            (
                "Search, filter, and manage every station in the OE5XRX network.",
                "Suche, filtere und verwalte jede Station im OE5XRX-Netz.",
            ),
            ("All tags", "Alle Tags"),
        ],
    ),
}

LOGIN_PAIRS = [
    ("Forgot password?", "Passwort vergessen?"),
    ("Callsign or username", "Rufzeichen oder Benutzername"),
]


def _url(name, lang, **kwargs):
    with translation.override(lang):
        return reverse(name, kwargs=kwargs or None)


def _logged_in(client, user, lang):
    user.language = lang
    user.save(update_fields=["language"])
    client.force_login(user)
    client.cookies[COOKIE] = lang
    return client


def _assert_lang(body, pairs, lang):
    for en, de in pairs:
        want, unwanted = (de, en) if lang == "de" else (en, de)
        assert escape(want) in body, f"{want!r} missing under /{lang}/"
        assert escape(unwanted) not in body, f"{unwanted!r} leaked into /{lang}/"


@pytest.mark.django_db
@pytest.mark.parametrize("lang", ["de", "en"])
@pytest.mark.parametrize("page", sorted(PAGES))
def test_page_renders_in_language(client, admin_user, page, lang):
    name, pairs = PAGES[page]
    _logged_in(client, admin_user, lang)
    resp = client.get(_url(name, lang))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert f'<html lang="{lang}"' in body
    _assert_lang(body, pairs, lang)


@pytest.mark.django_db
@pytest.mark.parametrize("lang", ["de", "en"])
def test_login_page_renders_in_language(client, lang):
    resp = client.get(_url("accounts:login", lang))
    assert resp.status_code == 200
    _assert_lang(resp.content.decode(), LOGIN_PAIRS, lang)


@pytest.mark.django_db
def test_js_catalog_german(client):
    body = client.get("/de/jsi18n/").content.decode()
    # djangojs.po: "Updating" -> "Wird aktualisiert", "Failed" -> "Fehlgeschlagen"
    assert '"Updating": "Wird aktualisiert"' in body
    assert '"Failed": "Fehlgeschlagen"' in body


@pytest.mark.django_db
def test_js_catalog_english_has_no_german(client):
    body = client.get("/en/jsi18n/").content.decode()
    assert "Wird aktualisiert" not in body


@pytest.mark.django_db
def test_profile_language_change_flash_is_in_new_language(client, member_user):
    """The success flash renders on the (already German) target page."""
    member_user.email = "member@example.org"
    member_user.save(update_fields=["email"])
    _logged_in(client, member_user, "en")
    resp = client.post(
        _url("accounts:profile", "en"),
        {
            "form_name": "identity",
            "identity-email": member_user.email,
            "identity-first_name": member_user.first_name,
            "identity-last_name": member_user.last_name,
            "identity-language": "de",
        },
    )
    assert resp.status_code == 302
    assert resp["Location"] == _url("accounts:profile", "de")
    texts = [str(m) for m in get_messages(resp.wsgi_request)]
    assert "Identität aktualisiert." in texts
    assert "Identity updated." not in texts


@pytest.mark.django_db
def test_profile_change_without_language_keeps_flash_language(client, member_user):
    member_user.email = "member@example.org"
    member_user.save(update_fields=["email"])
    _logged_in(client, member_user, "de")
    resp = client.post(
        _url("accounts:profile", "de"),
        {
            "form_name": "identity",
            "identity-email": member_user.email,
            "identity-first_name": "Max",
            "identity-last_name": member_user.last_name,
            "identity-language": "de",
        },
    )
    assert resp.status_code == 302
    texts = [str(m) for m in get_messages(resp.wsgi_request)]
    assert texts == ["Identität aktualisiert."]


# --- smoke: a broad set of UI pages renders under /de/ ------------------------

# url name -> kwargs key ("station" = the station fixture's pk) or None.
SMOKE_PAGES = {
    "dashboard:index": None,
    "accounts:profile": None,
    "accounts:notification_settings": None,
    "accounts:user_list": None,
    "accounts:user_create": None,
    "stations:station_list": None,
    "stations:station_detail": "station",
    "stations:station_edit": "station",
    "stations:region_list": None,
    "stations:region_create": None,
    "control:station_control": "station",
    "deployments:deployment_list": None,
    "deployments:deployment_create": None,
    "rollouts:upgrade_dashboard": None,
    "rollouts:sequence_edit": None,
    "images:list": None,
    "monitoring:alert_list": None,
    "monitoring:alert_settings": None,
    "module_firmware:module_list": None,
    "audit:audit_list": None,
    "repeaters:list": None,
    "repeaters:create": None,
    "sso:dashboard": None,
    "passive:aprs": None,
}


@pytest.mark.django_db
@pytest.mark.parametrize("name", sorted(SMOKE_PAGES))
def test_ui_page_renders_under_de(client, admin_user, station, name):
    _logged_in(client, admin_user, "de")
    kwarg = SMOKE_PAGES[name]
    kwargs = {"pk": station.pk} if kwarg == "station" else {}
    url = _url(name, "de", **kwargs)
    assert url.startswith("/de/")
    resp = client.get(url)
    assert resp.status_code == 200, f"{url} -> {resp.status_code}"
    assert '<html lang="de"' in resp.content.decode()
