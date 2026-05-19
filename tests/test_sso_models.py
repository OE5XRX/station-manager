import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone
from oauth2_provider.models import Application

from apps.sso.models import AppGrant

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
    return User.objects.create_user(username="alice", password="x", email="a@x")


@pytest.mark.django_db
def test_appgrant_is_active_by_default(alice, application):
    grant = AppGrant.objects.create(user=alice, application=application)
    assert grant.revoked_at is None


@pytest.mark.django_db
def test_appgrant_unique_per_user_per_app_while_active(alice, application):
    """Cannot create two active grants for the same (user, app)."""
    AppGrant.objects.create(user=alice, application=application)
    with pytest.raises(IntegrityError):
        AppGrant.objects.create(user=alice, application=application)


@pytest.mark.django_db
def test_appgrant_can_be_regranted_after_revoke(alice, application):
    """Once revoked, a new grant for the same (user, app) is allowed."""
    g1 = AppGrant.objects.create(user=alice, application=application)
    g1.revoked_at = timezone.now()
    g1.save()
    # No IntegrityError: partial index excludes revoked rows.
    g2 = AppGrant.objects.create(user=alice, application=application)
    assert g2.revoked_at is None
