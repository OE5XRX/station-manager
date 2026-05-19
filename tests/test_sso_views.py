"""Tests for the AppGrant toggle endpoint + dashboard views."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
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


@pytest.mark.django_db
def test_toggle_creates_grant_when_none_exists(client, admin_user, alice, app):
    client.force_login(admin_user)
    resp = client.post(
        f"/sso-admin/grants/toggle/{alice.pk}/{app.pk}/",
        follow=True,
    )
    assert resp.status_code == 200
    assert AppGrant.objects.filter(
        user=alice, application=app, revoked_at__isnull=True
    ).exists()


@pytest.mark.django_db
def test_toggle_revokes_grant_when_active_one_exists(client, admin_user, alice, app):
    AppGrant.objects.create(user=alice, application=app)
    client.force_login(admin_user)
    resp = client.post(
        f"/sso-admin/grants/toggle/{alice.pk}/{app.pk}/",
        follow=True,
    )
    assert resp.status_code == 200
    assert not AppGrant.objects.filter(
        user=alice, application=app, revoked_at__isnull=True
    ).exists()
    # Revoked record stays for audit:
    assert AppGrant.objects.filter(user=alice, application=app).count() == 1


@pytest.mark.django_db
def test_non_admin_cannot_toggle_grants(client, alice, app):
    client.force_login(alice)
    resp = client.post(
        f"/sso-admin/grants/toggle/{alice.pk}/{app.pk}/",
        follow=True,
    )
    # AdminRequiredMixin → 302 redirect to login, or 403 raise.
    assert resp.status_code in (302, 403, 200)  # 200 + redirect chain ends at login page
    # Either way: NO grant should be created.
    assert not AppGrant.objects.filter(user=alice, application=app).exists()


@pytest.mark.django_db
def test_toggle_writes_audit_log_entry(client, admin_user, alice, app):
    client.force_login(admin_user)
    client.post(
        f"/sso-admin/grants/toggle/{alice.pk}/{app.pk}/",
        follow=True,
    )
    entries = SsoAuditLog.objects.filter(
        actor=admin_user, target_user=alice, application=app
    )
    assert entries.exists()
    assert entries.first().event_type == SsoAuditLog.EventType.GRANT_GIVEN
