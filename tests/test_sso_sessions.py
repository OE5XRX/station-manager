"""Tests for TokenSession model — schema only in this task.

Lifecycle (validator hook, signals, admin-revoke) is covered in
later tasks.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from oauth2_provider.models import Application, RefreshToken

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
