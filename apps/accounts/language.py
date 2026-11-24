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


def language_for(user) -> str:
    """The language to write to ``user`` in: ``user.language`` if supported,
    else ``settings.LANGUAGE_CODE`` (also for ``None`` = a recipient without
    an account). Never the active language — that belongs to whoever
    triggered the message, not to the recipient."""
    return _supported_or_default(getattr(user, "language", None))


def _supported_or_default(language) -> str:
    return language if is_supported_language(language) else settings.LANGUAGE_CODE


def group_by_language(items, key=None) -> dict:
    """Group ``items`` by recipient language, preserving order.

    ``key(item)`` returns a user-like object or a language code; the default
    treats each item as the user itself. Lets a fan-out render a message once
    per language instead of once for everyone.
    """
    groups = {}
    for item in items:
        target = item if key is None else key(item)
        language = (
            _supported_or_default(target) if isinstance(target, str) else language_for(target)
        )
        groups.setdefault(language, []).append(item)
    return groups


def reverse_for_user(user, viewname, **kwargs):
    """``reverse`` under ``user.language`` — for links built without a request
    (push payloads, e-mails), which would otherwise carry the default prefix."""
    with override(language_for(user)):
        return reverse(viewname, **kwargs)
