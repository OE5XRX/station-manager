"""UI language ↔ ``User.language``.

One place that knows how the language cookie is written and how a URL is
moved to another locale prefix. Used by the login view, the language
switcher, the profile form and ``config.middleware.UserLanguageMiddleware``.
"""

from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.urls import reverse, translate_url
from django.utils.translation import get_language_from_path, override


def is_supported_language(language) -> bool:
    return language in dict(settings.LANGUAGES)


def set_language_cookie(response, language) -> bool:
    """Set the language cookie on ``response``; False (no-op) for unknown codes."""
    if not is_supported_language(language):
        return False
    response.set_cookie(
        settings.LANGUAGE_COOKIE_NAME,
        language,
        max_age=settings.LANGUAGE_COOKIE_AGE,
        path=settings.LANGUAGE_COOKIE_PATH,
        domain=settings.LANGUAGE_COOKIE_DOMAIN,
        secure=settings.LANGUAGE_COOKIE_SECURE,
        httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
        samesite=settings.LANGUAGE_COOKIE_SAMESITE,
    )
    return True


def localized_url(url: str, language: str) -> str:
    """``url`` with its locale prefix switched to ``language``.

    ``translate_url`` resolves the path under the *active* language, so a
    ``/de/...`` URL would not resolve while ``en`` is active (and be returned
    unchanged). Resolve under the URL's own prefix instead. Unprefixed or
    unresolvable URLs (e.g. ``/sso/authorize/?...``) come back unchanged.
    """
    if not is_supported_language(language):
        return url
    path_language = get_language_from_path(unquote(urlsplit(url).path))
    if path_language is None:
        return url
    with override(path_language):
        return translate_url(url, language)


def reverse_for_user(user, viewname, **kwargs):
    """``reverse`` under ``user.language`` — for links built without a request
    (push payloads, e-mails), which would otherwise carry the default prefix."""
    language = getattr(user, "language", None)
    if not is_supported_language(language):
        return reverse(viewname, **kwargs)
    with override(language):
        return reverse(viewname, **kwargs)
