"""Tests for the AppGrant toggle endpoint."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from oauth2_provider.models import Application

from apps.sso.models import AppGrant, ApplicationPolicy, SsoAuditLog

User = get_user_model()


@pytest.fixture
def admin_user(db):
    g, _ = Group.objects.get_or_create(name="admin")
    u = User.objects.create_user(username="admin", password="x", email="a@x.test")
    u.groups.add(g)
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
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
    assert AppGrant.objects.filter(user=alice, application=app, revoked_at__isnull=True).exists()


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
    entries = SsoAuditLog.objects.filter(actor=admin_user, target_user=alice, application=app)
    assert entries.exists()
    assert entries.first().event_type == SsoAuditLog.EventType.GRANT_GIVEN


@pytest.mark.django_db
def test_dashboard_lists_apps_with_grant_counts(client, admin_user, alice):
    from django.urls import reverse

    app1 = Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://i.example.org/cb/",
    )
    Application.objects.create(
        name="Grafana",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://g.example.org/cb/",
    )
    AppGrant.objects.create(user=alice, application=app1)

    client.force_login(admin_user)
    resp = client.get(reverse("sso:dashboard"))
    assert resp.status_code == 200
    assert b"InvenTree" in resp.content
    assert b"Grafana" in resp.content
    # InvenTree has 1 active grant, Grafana 0.
    # Loose check that "1" and "0" both appear in proximity to the app
    # names — the dashboard column renders the counts.
    body = resp.content.decode()
    inv_pos = body.find("InvenTree")
    graf_pos = body.find("Grafana")
    assert inv_pos != -1 and graf_pos != -1
    # The count column for InvenTree should contain a "1" within ~500 chars.
    assert "1" in body[inv_pos : inv_pos + 500]


@pytest.mark.django_db
def test_dashboard_requires_admin(client, alice):
    """Non-admins get 403."""
    from django.urls import reverse

    client.force_login(alice)
    resp = client.get(reverse("sso:dashboard"))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_dashboard_redirects_anonymous(client):
    from django.urls import reverse

    resp = client.get(reverse("sso:dashboard"))
    assert resp.status_code == 302
    assert "/accounts/login/" in resp["Location"]


@pytest.mark.django_db
def test_application_detail_lists_granted_and_not_granted_users(client, admin_user, alice, app):
    from django.urls import reverse

    # bob has no grant — created here as a fixture so the application
    # detail page can list him in the "without grant" column.
    User.objects.create_user(username="bob", password="x", email="b@x.test")
    AppGrant.objects.create(user=alice, application=app)

    client.force_login(admin_user)
    resp = client.get(reverse("sso:application_detail", kwargs={"pk": app.pk}))
    assert resp.status_code == 200
    assert b"alice" in resp.content
    assert b"bob" in resp.content
    assert b"InvenTree" in resp.content  # app name in heading


@pytest.mark.django_db
def test_application_detail_requires_admin(client, alice, app):
    from django.urls import reverse

    client.force_login(alice)
    resp = client.get(reverse("sso:application_detail", kwargs={"pk": app.pk}))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_toggle_from_app_detail_returns_hx_redirect(client, admin_user, alice, app):
    """When the toggle is invoked from the app-detail page (signalled via
    HX-Trigger-Name=from-app-detail), the response is an HX-Redirect back
    to the same page so both 'with grant' and 'without grant' columns
    refresh consistently."""
    from django.urls import reverse

    client.force_login(admin_user)
    resp = client.post(
        reverse("sso:grant_toggle", kwargs={"user_id": alice.pk, "application_id": app.pk}),
        HTTP_HX_TRIGGER_NAME="from-app-detail",
    )
    assert resp.status_code == 200
    assert "HX-Redirect" in resp.headers
    assert f"/sso-admin/applications/{app.pk}/" in resp.headers["HX-Redirect"]


@pytest.mark.django_db
def test_toggle_without_hx_trigger_returns_partial_as_before(client, admin_user, alice, app):
    """T13 toggle from the user-form page still gets the partial swap."""
    from django.urls import reverse

    client.force_login(admin_user)
    resp = client.post(
        reverse("sso:grant_toggle", kwargs={"user_id": alice.pk, "application_id": app.pk}),
    )
    assert resp.status_code == 200
    # The partial does not include the "HX-Redirect" header.
    assert "HX-Redirect" not in resp.headers
    # And it renders the app-grants-card div.
    assert b"sso-grants-card" in resp.content


# ---------------------------------------------------------------------------
# Task 5.1: SessionRevokeView
# ---------------------------------------------------------------------------


@pytest.fixture
def admin(db):
    u = User.objects.create_user(username="admin_revoke", password="x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    User._invalidate_role_cache(u)
    return u


@pytest.fixture
def session_row(db):
    from datetime import timedelta

    from django.utils import timezone
    from oauth2_provider.models import AccessToken, RefreshToken

    from apps.sso.models import TokenSession

    user = User.objects.create_user(username="target", password="x")
    app = Application.objects.create(
        name="InvenTreeSess",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://x.example.org/cb/",
    )
    at = AccessToken.objects.create(
        user=user,
        application=app,
        token="at1",
        expires=timezone.now() + timedelta(hours=1),
        scope="openid",
    )
    rt = RefreshToken.objects.create(
        user=user,
        application=app,
        token="rt1",
        access_token=at,
    )
    return TokenSession.objects.create(user=user, application=app, refresh_token=rt)


def test_session_revoke_view_requires_admin(db, client, session_row):
    user = User.objects.create_user(username="nonadmin", password="x")
    client.force_login(user)
    resp = client.post(reverse("sso:session_revoke", kwargs={"pk": session_row.pk}))
    assert resp.status_code == 403


def test_session_revoke_view_revokes(db, client, admin, session_row):
    from django.utils import timezone

    from apps.sso.models import TokenSession

    client.force_login(admin)
    resp = client.post(reverse("sso:session_revoke", kwargs={"pk": session_row.pk}))
    assert resp.status_code in (200, 302)

    session_row.refresh_from_db()
    assert session_row.revoked_at is not None
    assert session_row.revoked_by == admin
    assert session_row.revoke_reason == TokenSession.RevokeReason.ADMIN_REVOKE

    rt = session_row.refresh_token
    rt.refresh_from_db()
    assert rt.revoked is not None

    # Spec §4.4: the original AT (the one this RT was issued alongside)
    # must be expired by revoke, not just rotated children.
    at = session_row.refresh_token.access_token
    at.refresh_from_db()
    assert at.expires < timezone.now(), "Original AccessToken must be expired by revoke"

    log = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.SESSION_REVOKED,
        actor=admin,
        target_user=session_row.user,
    ).first()
    assert log is not None


def test_session_revoke_view_is_idempotent(db, client, admin, session_row):
    client.force_login(admin)
    client.post(reverse("sso:session_revoke", kwargs={"pk": session_row.pk}))
    # Second call: no second audit row, no error.
    client.post(reverse("sso:session_revoke", kwargs={"pk": session_row.pk}))
    log_count = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.SESSION_REVOKED,
        target_user=session_row.user,
    ).count()
    assert log_count == 1


# ---------------------------------------------------------------------------
# Task 5.2: ApplicationPolicyUpdateView
# ---------------------------------------------------------------------------


def test_app_policy_update_creates_row_if_missing(db, client, admin, session_row):
    app = session_row.application
    client.force_login(admin)
    resp = client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "open_to_members"},
    )
    assert resp.status_code in (200, 302)
    pol = ApplicationPolicy.objects.get(application=app)
    assert pol.access_policy == "open_to_members"
    assert pol.modified_by == admin


def test_app_policy_update_emits_audit_with_old_and_new(db, client, admin, session_row):
    app = session_row.application
    ApplicationPolicy.objects.create(application=app, access_policy="grant_required")
    client.force_login(admin)
    client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "open_to_all"},
    )
    log = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.APP_POLICY_CHANGED,
        application=app,
    ).first()
    assert log is not None
    assert "grant_required" in log.message
    assert "open_to_all" in log.message


def test_app_policy_update_rejects_unknown_policy(db, client, admin, session_row):
    app = session_row.application
    client.force_login(admin)
    resp = client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "not-a-real-policy"},
    )
    assert resp.status_code == 400


def test_app_policy_update_requires_admin(db, client, session_row):
    app = session_row.application
    user = User.objects.create_user(username="member-only", password="x")
    client.force_login(user)
    resp = client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "open_to_all"},
    )
    assert resp.status_code == 403


def test_app_policy_update_noop_skips_audit(db, client, admin, session_row):
    """Posting the same policy twice should produce exactly one audit row
    (from the first set), not two."""
    app = session_row.application
    client.force_login(admin)
    client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "open_to_all"},
    )
    client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "open_to_all"},
    )
    log_count = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.APP_POLICY_CHANGED,
        application=app,
    ).count()
    assert log_count == 1, "No-op repost must not produce a second audit row"


# ---------------------------------------------------------------------------
# Task 5.4: Dashboard KPI tile + policy column
# ---------------------------------------------------------------------------


def test_dashboard_shows_active_sessions_count(db, client, admin, session_row):
    client.force_login(admin)
    resp = client.get(reverse("sso:dashboard"))
    assert resp.status_code == 200
    assert b"Active sessions" in resp.content
    # The session_row fixture creates exactly one active session.
    assert b">1<" in resp.content or b">1 " in resp.content


def test_dashboard_shows_policy_badge(db, client, admin, session_row):
    app = session_row.application
    ApplicationPolicy.objects.create(application=app, access_policy="open_to_members")
    client.force_login(admin)
    resp = client.get(reverse("sso:dashboard"))
    assert resp.status_code == 200
    assert b"Open to members" in resp.content
