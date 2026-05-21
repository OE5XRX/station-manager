"""Unit tests for AppGrant access control in the OIDC flow.

These target the pure function `user_can_access` directly — the
integration with DOT's authorization pipeline is exercised
end-to-end in tests/test_sso_flow.py (Task 18).
"""

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from oauth2_provider.models import Application

from apps.sso.models import AppGrant
from apps.sso.permissions import user_can_access

User = get_user_model()


@pytest.fixture
def application(db):
    return Application.objects.create(
        name="InvenTree-Test",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="x", email="a@x.test")


@pytest.mark.django_db
def test_user_with_active_grant_is_allowed(alice, application):
    AppGrant.objects.create(user=alice, application=application)
    assert user_can_access(alice, application) is True


@pytest.mark.django_db
def test_user_without_grant_is_denied(alice, application):
    assert user_can_access(alice, application) is False


@pytest.mark.django_db
def test_inactive_user_is_denied_even_with_grant(alice, application):
    AppGrant.objects.create(user=alice, application=application)
    alice.is_active = False
    alice.save()
    assert user_can_access(alice, application) is False


@pytest.mark.django_db
def test_revoked_grant_is_denied(alice, application):
    grant = AppGrant.objects.create(user=alice, application=application)
    grant.revoked_at = timezone.now()
    grant.save()
    assert user_can_access(alice, application) is False


@pytest.mark.django_db
def test_user_with_grant_for_other_app_is_denied_for_this_app(alice, application):
    """User has an active grant for one app, but is asking for a different app."""
    other_app = Application.objects.create(
        name="Other",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://other.example.org/cb/",
    )
    AppGrant.objects.create(user=alice, application=other_app)
    assert user_can_access(alice, application) is False
