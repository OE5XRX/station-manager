import pytest
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application

from apps.sso.models import SsoAuditLog

User = get_user_model()


@pytest.mark.django_db
def test_ssoauditlog_records_grant_given_event():
    admin = User.objects.create_user(username="admin", password="x", email="a@x.test")
    target = User.objects.create_user(username="target", password="x", email="t@x.test")
    app = Application.objects.create(
        name="X",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://x.example.org/cb/",
    )

    entry = SsoAuditLog.log(
        event_type=SsoAuditLog.EventType.GRANT_GIVEN,
        actor=admin,
        target_user=target,
        application=app,
        message="grant added",
    )
    assert entry.pk is not None
    assert entry.event_type == "grant_given"
    assert entry.actor_id == admin.pk
    assert entry.target_user_id == target.pk
    assert entry.application_id == app.pk


@pytest.mark.django_db
def test_ssoauditlog_log_helper_accepts_minimal_kwargs():
    """No actor / target / application — system-internal event."""
    entry = SsoAuditLog.log(
        event_type=SsoAuditLog.EventType.LOGIN_DENIED_NO_GRANT,
        message="login denied (no grant)",
    )
    assert entry.pk is not None
    assert entry.actor is None
    assert entry.target_user is None
    assert entry.application is None


def test_audit_event_type_includes_session_revoked():
    assert SsoAuditLog.EventType.SESSION_REVOKED == "session_revoked"


def test_audit_event_type_includes_app_policy_changed():
    assert SsoAuditLog.EventType.APP_POLICY_CHANGED == "app_policy_changed"


def test_audit_event_type_includes_group_membership_changed():
    assert SsoAuditLog.EventType.GROUP_MEMBERSHIP_CHANGED == "group_membership_changed"
