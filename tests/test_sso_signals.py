"""Cascading token revocation: User.is_active=False and AppGrant.revoke
both force-expire DOT's AccessToken/RefreshToken rows.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, RefreshToken

from apps.sso.models import AppGrant

User = get_user_model()


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="App-A",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://a.example.org/cb/",
    )


@pytest.fixture
def other_app(db):
    return Application.objects.create(
        name="App-B",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://b.example.org/cb/",
    )


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="x", email="a@x.test")


def _create_token(user, application, token_value):
    """Helper: create matched Access+Refresh tokens for (user, app)."""
    now = timezone.now()
    access = AccessToken.objects.create(
        user=user,
        application=application,
        token=token_value,
        expires=now + timedelta(hours=1),
        scope="openid",
    )
    refresh = RefreshToken.objects.create(
        user=user,
        application=application,
        token=f"refresh-{token_value}",
        access_token=access,
    )
    return access, refresh


@pytest.mark.django_db
def test_deactivating_user_revokes_all_their_access_tokens(alice, app, other_app):
    a1, _ = _create_token(alice, app, "tok-a")
    a2, _ = _create_token(alice, other_app, "tok-b")

    alice.is_active = False
    alice.save()

    now = timezone.now()
    a1.refresh_from_db()
    a2.refresh_from_db()
    assert a1.expires <= now
    assert a2.expires <= now


@pytest.mark.django_db
def test_deactivating_user_revokes_refresh_tokens(alice, app):
    _, r1 = _create_token(alice, app, "tok-x")

    alice.is_active = False
    alice.save()

    r1.refresh_from_db()
    assert r1.revoked is not None


@pytest.mark.django_db
def test_deactivating_user_writes_audit_log(alice, app):
    from apps.sso.models import SsoAuditLog

    _create_token(alice, app, "tok-y")

    alice.is_active = False
    alice.save()

    entries = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.TOKEN_REVOKED,
        target_user=alice,
    )
    assert entries.exists()


@pytest.mark.django_db
def test_revoking_appgrant_revokes_tokens_only_for_that_app(alice, app, other_app):
    """AppGrant revoke targets tokens for (user, that_app), NOT other apps."""
    a_app, _ = _create_token(alice, app, "tok-here")
    a_other, _ = _create_token(alice, other_app, "tok-elsewhere")

    grant = AppGrant.objects.create(user=alice, application=app)
    grant.revoked_at = timezone.now()
    grant.save()

    now = timezone.now()
    a_app.refresh_from_db()
    a_other.refresh_from_db()
    assert a_app.expires <= now
    assert a_other.expires > now  # untouched


@pytest.mark.django_db
def test_creating_a_new_appgrant_does_not_revoke_anything(alice, app):
    """Only the False -> True transition on revoked_at triggers the cascade."""
    a, _ = _create_token(alice, app, "tok-fresh")

    AppGrant.objects.create(user=alice, application=app)
    # No revoke happened. Tokens must still be valid.
    a.refresh_from_db()
    assert a.expires > timezone.now()


@pytest.mark.django_db
def test_creating_an_active_user_does_not_revoke_anything(app):
    """New User creation must not trigger token revocation
    (no tokens exist for a brand-new user, but the signal handler
    should bail out cleanly without spurious work)."""
    User.objects.create_user(username="brand_new", password="x", email="n@x.test")
    # No exception, no audit log entry for token revoke.
    from apps.sso.models import SsoAuditLog
    assert not SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.TOKEN_REVOKED
    ).exists()
