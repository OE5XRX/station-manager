"""Pins the webpush feature-flag semantics: disabled unless VAPID keys set."""
from django.conf import settings


def test_webpush_disabled_without_keys(monkeypatch):
    # Default test env sets no VAPID keys → channel is off.
    assert settings.ALERT_WEBPUSH_ENABLED is False


def test_webpush_settings_names_exist():
    assert hasattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY")
    assert hasattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY")
    assert hasattr(settings, "WEBPUSH_VAPID_ADMIN_EMAIL")
