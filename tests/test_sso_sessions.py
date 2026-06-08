"""Tests for TokenSession model — schema only in this task.

Lifecycle (validator hook, signals, admin-revoke) is covered in
later tasks.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, RefreshToken

from apps.accounts.models import User
from apps.sso.models import SsoAuditLog, TokenSession
from apps.sso.permissions import SsoOAuth2Validator


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(username="peter", password="x")


def test_token_session_minimal_fields(db, user, app):
    s = TokenSession.objects.create(user=user, application=app)
    assert s.revoked_at is None
    assert s.ip_address is None
    assert s.user_agent == ""
    assert s.country_code == ""
    assert s.city == ""
    assert s.parent is None
    assert s.revoked_by is None


def test_token_session_revoke_reason_choices(db):
    choices = {value for value, _ in TokenSession.RevokeReason.choices}
    assert choices == {
        "admin_revoke",
        "user_logout",
        "user_deactivated",
        "grant_revoked",
        "rotated",
    }


def test_token_session_parent_self_reference(db, user, app):
    parent = TokenSession.objects.create(user=user, application=app)
    child = TokenSession.objects.create(user=user, application=app, parent=parent)
    assert child.parent == parent
    assert list(parent.children.all()) == [child]


def test_token_session_is_active_property(db, user, app):
    s = TokenSession.objects.create(user=user, application=app)
    # No refresh_token attached: not active.
    assert s.is_active is False

    # Revoked: not active.
    s.revoked_at = timezone.now()
    s.save(update_fields=["revoked_at"])
    assert s.is_active is False


def test_token_session_is_active_with_live_refresh_token(db, user, app):
    """Positive path: revoked_at None + live refresh_token + within lifetime => True."""
    at = AccessToken.objects.create(
        user=user, application=app, token="at-1",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
    )
    rt = RefreshToken.objects.create(
        user=user, application=app, token="rt-1", access_token=at,
    )
    s = TokenSession.objects.create(user=user, application=app, refresh_token=rt)
    assert s.is_active is True


def test_token_session_is_active_false_when_refresh_token_revoked(db, user, app):
    """Negative path: refresh_token.revoked is set => False."""
    at = AccessToken.objects.create(
        user=user, application=app, token="at-2",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
    )
    rt = RefreshToken.objects.create(
        user=user, application=app, token="rt-2", access_token=at,
        revoked=timezone.now(),
    )
    s = TokenSession.objects.create(user=user, application=app, refresh_token=rt)
    assert s.is_active is False


def test_token_session_is_active_false_when_lifetime_exceeded(db, user, app):
    """Negative path: issued_at older than REFRESH_TOKEN_EXPIRE_SECONDS => False."""
    at = AccessToken.objects.create(
        user=user, application=app, token="at-3",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
    )
    rt = RefreshToken.objects.create(
        user=user, application=app, token="rt-3", access_token=at,
    )
    s = TokenSession.objects.create(user=user, application=app, refresh_token=rt)
    # Backdate issued_at via .update() to bypass auto_now_add
    TokenSession.objects.filter(pk=s.pk).update(
        issued_at=timezone.now() - timedelta(days=15),
    )
    s.refresh_from_db()
    assert s.is_active is False


def _make_dot_tokens(user, app):
    """Create AccessToken + RefreshToken via DOT's models as if save_bearer_token
    had just run super(). The validator hook attaches metadata afterwards."""
    at = AccessToken.objects.create(
        user=user, application=app,
        token="atok-123",
        expires=timezone.now() + timedelta(hours=1),
        scope="openid",
    )
    rt = RefreshToken.objects.create(
        user=user, application=app, token="rtok-456",
        access_token=at,
    )
    return at, rt


def test_save_bearer_token_creates_token_session(db, user, app):
    _, rt = _make_dot_tokens(user, app)
    request = SimpleNamespace(
        headers={"X-Forwarded-For": "89.207.4.5", "User-Agent": "TestUA/1.0"},
        refresh_token_instance=None,
    )

    validator = SsoOAuth2Validator()
    validator._record_token_session({"refresh_token": "rtok-456"}, request)

    s = TokenSession.objects.get(refresh_token=rt)
    assert s.user == user
    assert s.application == app
    assert s.ip_address == "89.207.4.5"
    assert s.user_agent == "TestUA/1.0"
    assert s.parent is None
    assert s.revoked_at is None


def test_save_bearer_token_emits_login_success_audit(db, user, app):
    _, rt = _make_dot_tokens(user, app)
    request = SimpleNamespace(
        headers={"X-Real-IP": "89.207.4.5"},
        refresh_token_instance=None,
    )
    validator = SsoOAuth2Validator()
    validator._record_token_session({"refresh_token": "rtok-456"}, request)

    log = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
        target_user=user, application=app,
    ).first()
    assert log is not None
    assert log.ip_address == "89.207.4.5"


def test_save_bearer_token_refresh_rotation_chains_parent(db, user, app):
    _, parent_rt = _make_dot_tokens(user, app)
    validator = SsoOAuth2Validator()
    validator._record_token_session(
        {"refresh_token": "rtok-456"},
        SimpleNamespace(headers={"X-Real-IP": "89.207.4.5", "User-Agent": "UA1"},
                        refresh_token_instance=None),
    )
    parent_session = TokenSession.objects.get(refresh_token=parent_rt)

    # Simulate rotation: DOT creates a new RefreshToken; we feed it in.
    at2 = AccessToken.objects.create(
        user=user, application=app, token="atok-789",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
    )
    rt2 = RefreshToken.objects.create(
        user=user, application=app, token="rtok-789", access_token=at2,
    )

    validator._record_token_session(
        {"refresh_token": "rtok-789"},
        SimpleNamespace(headers={"X-Real-IP": "89.207.4.5", "User-Agent": "UA1"},
                        refresh_token_instance=parent_rt),
    )

    child_session = TokenSession.objects.get(refresh_token=rt2)
    assert child_session.parent == parent_session

    parent_session.refresh_from_db()
    assert parent_session.revoked_at is not None
    assert parent_session.revoke_reason == TokenSession.RevokeReason.ROTATED


def test_save_bearer_token_geoip_fallback_writes_empty_fields(db, user, app, monkeypatch):
    """When GeoIP DB is missing, country/city stay empty -- session row is
    still created, login is not blocked."""
    _, rt = _make_dot_tokens(user, app)
    request = SimpleNamespace(headers={"X-Real-IP": "203.0.113.99"},
                              refresh_token_instance=None)

    # Monkeypatch lookup_location at the geoip module (the import target
    # of the local ``from .geoip import lookup_location`` inside
    # ``_record_token_session``). This avoids mutating module-level
    # singleton state in apps.sso.geoip; pytest auto-restores on teardown
    # so a failing assert can't leak GeoIP-disabled state into other tests.
    from apps.sso import geoip as geoip_mod
    monkeypatch.setattr(geoip_mod, "lookup_location",
                        lambda _ip: (None, None), raising=True)

    validator = SsoOAuth2Validator()
    validator._record_token_session({"refresh_token": "rtok-456"}, request)
    s = TokenSession.objects.get(refresh_token=rt)
    assert s.country_code == ""
    assert s.city == ""
    assert s.ip_address == "203.0.113.99"


def test_save_bearer_token_rotation_falls_back_to_source_refresh_token(db, user, app):
    """Cover the production rotation path where DOT clears request.refresh_token_instance.

    DOT's oauth2_validators._save_bearer_token sets refresh_token_instance to None
    after the parent's revoke completes. Our recorder must then fall back to
    walking new_rt.access_token.source_refresh_token (which DOT wires reliably at
    _create_access_token).
    """
    _, parent_rt = _make_dot_tokens(user, app)
    validator = SsoOAuth2Validator()
    validator._record_token_session(
        {"refresh_token": "rtok-456"},
        SimpleNamespace(headers={"X-Real-IP": "89.207.4.5"}, refresh_token_instance=None),
    )
    parent_session = TokenSession.objects.get(refresh_token=parent_rt)

    # Simulate DOT post-revoke state: attribute cleared, source_refresh_token wired.
    at2 = AccessToken.objects.create(
        user=user, application=app, token="atok-789",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
        source_refresh_token=parent_rt,
    )
    rt2 = RefreshToken.objects.create(
        user=user, application=app, token="rtok-789", access_token=at2,
    )

    validator._record_token_session(
        {"refresh_token": "rtok-789"},
        SimpleNamespace(headers={"X-Real-IP": "89.207.4.5"}, refresh_token_instance=None),
    )

    child_session = TokenSession.objects.get(refresh_token=rt2)
    assert child_session.parent == parent_session, (
        "Fallback to source_refresh_token must wire parent FK"
    )

    parent_session.refresh_from_db()
    assert parent_session.revoked_at is not None, "Parent must be marked revoked"
    assert parent_session.revoke_reason == TokenSession.RevokeReason.ROTATED
