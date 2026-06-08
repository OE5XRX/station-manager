"""Tests for TokenSession model — schema only in this task.

Lifecycle (validator hook, signals, admin-revoke) is covered in
later tasks.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, RefreshToken

from apps.accounts.models import User
from apps.sso.models import TokenSession


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
