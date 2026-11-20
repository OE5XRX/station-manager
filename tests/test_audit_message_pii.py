"""Audit messages must not carry e-mail addresses, IPs, user agents or GeoIP cities.

The IP lives only in the dedicated ``ip_address`` column (90-day retention);
the free-text ``message`` is kept for 365 days and must stay free of it.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, RefreshToken

from apps.accounts.models import AccountAuditLog, AccountToken, User
from apps.sso.models import SsoAuditLog, TokenSession
from apps.sso.permissions import SsoOAuth2Validator


@pytest.fixture(autouse=True)
def _trust_cf_header(settings):
    """Prod config: CF-Connecting-IP is the trusted client-IP header."""
    settings.CLIENT_IP_HEADER = "CF-Connecting-IP"


EMAIL = "member@example.org"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        email="admin@example.org",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM",
        email=EMAIL,
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


def _assert_clean(row, *needles):
    for needle in ("@", "127.0.0.1", *needles):
        assert needle not in row.message, (row.event_type, row.message)


def _row(event_type, **filters):
    return AccountAuditLog.objects.filter(event_type=event_type, **filters).latest("created_at")


@pytest.mark.django_db
def test_password_reset_audit_has_no_email_or_ip(client, member):
    client.post(reverse("accounts:password_reset_request"), {"email": EMAIL})
    row = _row(AccountAuditLog.EventType.PASSWORD_RESET_REQUESTED)
    _assert_clean(row)
    assert row.message == "reset link sent"
    assert row.ip_address == "127.0.0.1"


@pytest.mark.django_db
def test_password_reset_rate_limited_audit_has_no_email(client, member):
    for _ in range(12):
        client.post(reverse("accounts:password_reset_request"), {"email": EMAIL})
    row = _row(AccountAuditLog.EventType.PASSWORD_RESET_RATE_LIMITED)
    _assert_clean(row)
    assert row.message.startswith("rate limited (")
    assert row.ip_address == "127.0.0.1"


@pytest.mark.django_db
def test_user_create_audit_has_no_email(client, admin):
    client.force_login(admin)
    client.post(
        reverse("accounts:user_create"),
        {
            "username": "OE5NEW",
            "email": "new@example.org",
            "first_name": "",
            "last_name": "",
            "language": "en",
        },
    )
    new = User.objects.get(username="OE5NEW")
    created = _row(AccountAuditLog.EventType.USER_CREATED, target_user=new)
    assert created.message == "OE5NEW"
    sent = _row(AccountAuditLog.EventType.WELCOME_TOKEN_SENT, target_user=new)
    _assert_clean(sent)
    assert sent.message == "welcome link sent"


@pytest.mark.django_db
def test_resend_welcome_audit_has_no_email(client, admin):
    user = User.objects.create_user(
        username="OE5NEW",
        email="new@example.org",
        membership_level=User.MembershipLevel.APPLICANT,
    )
    user.set_unusable_password()
    user.save()
    client.force_login(admin)
    client.post(reverse("accounts:resend_welcome", kwargs={"pk": user.pk}))
    row = _row(AccountAuditLog.EventType.WELCOME_TOKEN_SENT, target_user=user)
    _assert_clean(row)
    assert row.message == "welcome link re-sent"


@pytest.mark.django_db
def test_email_verify_requested_and_verified_have_no_email(client, member):
    from apps.accounts.tokens import issue_token

    client.force_login(member)
    client.post(
        reverse("accounts:profile"),
        {
            "form_name": "identity",
            "identity-email": "new@example.org",
            "identity-first_name": "Hans",
            "identity-last_name": "",
            "identity-language": "en",
        },
    )
    requested = _row(AccountAuditLog.EventType.EMAIL_VERIFY_REQUESTED, target_user=member)
    _assert_clean(requested)
    assert requested.message == "email change requested"

    raw = issue_token(
        member, AccountToken.TokenType.VERIFY, payload={"new_email": "new2@example.org"}
    )
    client.get(reverse("accounts:verify_email", kwargs={"token": raw}))
    verified = _row(AccountAuditLog.EventType.EMAIL_VERIFIED, target_user=member)
    _assert_clean(verified)
    assert verified.message == "email changed"


@pytest.mark.django_db
def test_soft_delete_restore_purge_audit_have_no_email(client, admin, member):
    client.force_login(admin)
    client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
    soft = _row(AccountAuditLog.EventType.USER_SOFT_DELETED, target_user=member)
    assert soft.message == "OE5MEM"

    client.post(reverse("accounts:user_restore", kwargs={"pk": member.pk}))
    restored = _row(AccountAuditLog.EventType.USER_RESTORED, target_user=member)
    assert restored.message == "OE5MEM"

    client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
    member.refresh_from_db()
    soft_date = member.deleted_at
    client.post(reverse("accounts:user_hard_purge", kwargs={"pk": member.pk}))
    purged = _row(AccountAuditLog.EventType.USER_HARD_PURGED)
    assert purged.message == f"OE5MEM (purged after soft-delete on {soft_date:%Y-%m-%d})"
    for row in AccountAuditLog.objects.all():
        assert EMAIL not in row.message


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


def _issue_tokens(user, app):
    at = AccessToken.objects.create(
        user=user,
        application=app,
        token="atok-pii",
        expires=timezone.now() + timedelta(hours=1),
        scope="openid",
    )
    return RefreshToken.objects.create(
        user=user, application=app, token="rtok-pii", access_token=at
    )


@pytest.mark.django_db
def test_sso_login_success_message_has_no_ua_city(member, app, monkeypatch):
    monkeypatch.setattr("apps.sso.geoip.lookup_location", lambda ip: ("AT", "Linz"))
    _issue_tokens(member, app)
    request = SimpleNamespace(
        headers={"CF-Connecting-IP": "89.207.4.5", "User-Agent": "TestUA/1.0"},
        refresh_token_instance=None,
    )
    SsoOAuth2Validator()._record_token_session({"refresh_token": "rtok-pii"}, request)

    row = SsoAuditLog.objects.get(event_type=SsoAuditLog.EventType.LOGIN_SUCCESS)
    assert row.message == "Token issued."
    assert row.ip_address == "89.207.4.5"
    session = TokenSession.objects.get(user=member)
    assert session.user_agent == "TestUA/1.0"
    assert session.city == "Linz"


@pytest.mark.django_db
def test_sso_session_revoked_message_has_no_ip_city(client, admin, member, app):
    rt = _issue_tokens(member, app)
    session = TokenSession.objects.create(
        user=member,
        application=app,
        refresh_token=rt,
        ip_address="89.207.4.5",
        city="Linz",
    )
    client.force_login(admin)
    client.post(reverse("sso:session_revoke", kwargs={"pk": session.pk}))

    row = SsoAuditLog.objects.get(event_type=SsoAuditLog.EventType.SESSION_REVOKED)
    session.refresh_from_db()
    assert row.message == f"Session {session.pk} revoked. Issued {session.issued_at.isoformat()}."
    assert "89.207.4.5" not in row.message
    assert "Linz" not in row.message
    assert row.ip_address == "127.0.0.1"


def test_no_username_in_station_audit_messages():
    """Station-audit messages of non-personal event types outlive a hard purge
    (user FK → NULL), so the actor must only live in the ``user`` FK, never in
    the free text. Guard against ``... by {user.username}``-style messages.
    AccountAuditLog suffixes (``suffix=``) are exempt: that table is deleted
    after RETENTION_AUDIT_DAYS in full."""
    import re
    from pathlib import Path

    pattern = re.compile(r"by \{(?:self\.)?(?:request\.)?user(?:\.username)?\}")
    offenders = []
    for path in Path("apps").rglob("*.py"):
        if path.parts[1] in {"accounts", "sso"}:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line) and "suffix=" not in line:
                offenders.append(f"{path}:{lineno}")
    assert offenders == []
