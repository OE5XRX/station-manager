"""Recipient-language helpers for messages built without a request."""

from types import SimpleNamespace

import pytest
from django.conf import settings
from django.utils import translation

from apps.accounts.language import group_by_language, language_for, reverse_for_user


def _u(language):
    return SimpleNamespace(language=language)


def test_language_for_supported_language():
    assert language_for(_u("de")) == "de"
    assert language_for(_u("en")) == "en"


@pytest.mark.parametrize("language", ["", None, "fr", "xx-yy"])
def test_language_for_unsupported_falls_back_to_language_code(language):
    assert language_for(_u(language)) == settings.LANGUAGE_CODE


def test_language_for_no_user_falls_back_to_language_code():
    assert language_for(None) == settings.LANGUAGE_CODE


def test_language_for_ignores_active_language():
    with translation.override("de"):
        assert language_for(None) == settings.LANGUAGE_CODE


def test_group_by_language_users_in_stable_order():
    a, b, c, d = _u("de"), _u("en"), _u("de"), _u("")
    groups = group_by_language([a, b, c, d])
    assert groups == {"de": [a, c], "en": [b, d]}
    assert list(groups) == ["de", "en"]


def test_group_by_language_with_key_returning_code():
    rows = [("x@a", "de"), ("y@a", "en"), ("z@a", "de"), ("w@a", "fr")]
    groups = group_by_language(rows, key=lambda row: row[1])
    assert groups == {"de": [("x@a", "de"), ("z@a", "de")], "en": [("y@a", "en"), ("w@a", "fr")]}


def test_reverse_for_user_unsupported_language_uses_language_code_not_active():
    with translation.override("de"):
        url = reverse_for_user(_u("fr"), "monitoring:alert_list")
    assert url.startswith(f"/{settings.LANGUAGE_CODE}/")


def test_reverse_for_user_uses_user_language():
    with translation.override("en"):
        assert reverse_for_user(_u("de"), "monitoring:alert_list").startswith("/de/")
