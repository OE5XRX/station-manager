"""Tests for the AppGrant toggle endpoint."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from oauth2_provider.models import Application

from apps.sso.models import AppGrant, SsoAuditLog

User = get_user_model()


@pytest.fixture
def admin_user(db):
    g, _ = Group.objects.get_or_create(name="admin")
    u = User.objects.create_user(username="admin", password="x", email="a@x.test")
    u.groups.add(g)
    return u


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="x", email="al@x.test")


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://x.example.org/cb/",
    )


def _toggle_url(user, application):
    """reverse() honors i18n_patterns so we get /en/sso-admin/... not /sso-admin/...
    which would otherwise get 302-redirected by LocaleMiddleware to a GET."""
    return reverse(
        "sso:grant_toggle",
        kwargs={"user_id": user.pk, "application_id": application.pk},
    )


@pytest.mark.django_db
def test_toggle_creates_grant_when_none_exists(client, admin_user, alice, app):
    client.force_login(admin_user)
    resp = client.post(_toggle_url(alice, app))
    assert resp.status_code == 200
    assert AppGrant.objects.filter(
        user=alice, application=app, revoked_at__isnull=True
    ).exists()


@pytest.mark.django_db
def test_toggle_revokes_grant_when_active_one_exists(client, admin_user, alice, app):
    AppGrant.objects.create(user=alice, application=app)
    client.force_login(admin_user)
    resp = client.post(_toggle_url(alice, app))
    assert resp.status_code == 200
    assert not AppGrant.objects.filter(
        user=alice, application=app, revoked_at__isnull=True
    ).exists()
    # Soft delete preserves the revoked row for audit.
    assert AppGrant.objects.filter(user=alice, application=app).count() == 1


@pytest.mark.django_db
def test_non_admin_cannot_toggle_grants(client, alice, app):
    """A logged-in non-admin user must be denied. The AdminOnlyMixin uses
    UserPassesTestMixin which, for an authenticated-but-failing user,
    raises PermissionDenied -> 403."""
    client.force_login(alice)
    resp = client.post(_toggle_url(alice, app))
    assert resp.status_code == 403
    assert not AppGrant.objects.filter(user=alice, application=app).exists()


@pytest.mark.django_db
def test_anonymous_cannot_toggle_grants(client, alice, app):
    """Anonymous request must be redirected to login (LoginRequiredMixin)."""
    resp = client.post(_toggle_url(alice, app))
    assert resp.status_code == 302
    assert "/accounts/login/" in resp["Location"]
    assert not AppGrant.objects.filter(user=alice, application=app).exists()


@pytest.mark.django_db
def test_toggle_writes_audit_log_entry(client, admin_user, alice, app):
    client.force_login(admin_user)
    client.post(_toggle_url(alice, app))
    entries = SsoAuditLog.objects.filter(
        actor=admin_user, target_user=alice, application=app
    )
    assert entries.exists()
    assert entries.first().event_type == SsoAuditLog.EventType.GRANT_GIVEN
