"""Sub-Spec 2a — Password Reset flow (form, view, rate limits, set-password).

Sub-Spec 2a §3.2 + §8.2.
"""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountToken, User


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        email="m@example.org",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the LocMemCache between tests so rate-limit counters don't leak."""
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestIpRateLimit:
    def test_under_limit_allows(self):
        from apps.accounts.throttle import _ip_rate_exceeded

        for _ in range(9):
            assert _ip_rate_exceeded("127.0.0.1") is False

    def test_tenth_is_still_allowed_then_blocked(self):
        from apps.accounts.throttle import _ip_rate_exceeded

        for _ in range(10):
            assert _ip_rate_exceeded("127.0.0.1") is False
        # 11th attempt is over the limit
        assert _ip_rate_exceeded("127.0.0.1") is True

    def test_different_ips_isolated(self):
        from apps.accounts.throttle import _ip_rate_exceeded

        for _ in range(10):
            _ip_rate_exceeded("127.0.0.1")
        # other IP still has full quota
        assert _ip_rate_exceeded("127.0.0.2") is False


@pytest.mark.django_db
class TestUserRateLimit:
    def test_under_limit_allows(self, member):
        from apps.accounts.throttle import _user_rate_exceeded

        assert _user_rate_exceeded(member) is False

    def test_three_recent_tokens_block_fourth(self, member):
        from apps.accounts.throttle import _user_rate_exceeded

        for _ in range(3):
            AccountToken.objects.create(
                user=member,
                token_type=AccountToken.TokenType.RESET,
                secret_hash="x" * 64,
                expires_at=timezone.now() + timedelta(hours=24),
            )
        assert _user_rate_exceeded(member) is True

    def test_old_tokens_dont_count(self, member):
        from apps.accounts.throttle import _user_rate_exceeded

        for _ in range(3):
            t = AccountToken.objects.create(
                user=member,
                token_type=AccountToken.TokenType.RESET,
                secret_hash="x" * 64,
                expires_at=timezone.now() + timedelta(hours=24),
            )
            AccountToken.objects.filter(pk=t.pk).update(
                created_at=timezone.now() - timedelta(hours=2)
            )
        assert _user_rate_exceeded(member) is False

    def test_other_token_types_dont_count(self, member):
        from apps.accounts.throttle import _user_rate_exceeded

        for _ in range(5):
            AccountToken.objects.create(
                user=member,
                token_type=AccountToken.TokenType.WELCOME,
                secret_hash="x" * 64,
                expires_at=timezone.now() + timedelta(days=7),
            )
        assert _user_rate_exceeded(member) is False


@pytest.mark.django_db
class TestPasswordResetRequestView:
    def test_get_renders_form(self, client):
        resp = client.get(reverse("accounts:password_reset_request"))
        assert resp.status_code == 200
        assert b"email" in resp.content.lower()

    def test_post_existing_email_issues_reset_token(self, client, member):
        client.post(
            reverse("accounts:password_reset_request"),
            {"email": member.email},
        )
        assert AccountToken.objects.filter(
            user=member,
            token_type=AccountToken.TokenType.RESET,
            used_at__isnull=True,
        ).exists()

    def test_post_existing_email_sends_mail(self, client, member):
        from django.core import mail

        client.post(
            reverse("accounts:password_reset_request"),
            {"email": member.email},
        )
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [member.email]
        assert "Password reset" in mail.outbox[0].subject

    def test_post_nonexistent_email_no_token_no_mail(self, client, db):
        from django.core import mail

        client.post(
            reverse("accounts:password_reset_request"),
            {"email": "nobody@example.org"},
        )
        assert not AccountToken.objects.exists()
        assert len(mail.outbox) == 0

    def test_post_nonexistent_email_same_response_as_existing(self, client, member, db):
        resp_known = client.post(
            reverse("accounts:password_reset_request"),
            {"email": member.email},
        )
        resp_unknown = client.post(
            reverse("accounts:password_reset_request"),
            {"email": "nobody@example.org"},
        )
        assert resp_known.status_code == 302
        assert resp_unknown.status_code == 302
        assert resp_known.url == resp_unknown.url

    def test_per_user_limit_blocks_fourth(self, client, member):
        from django.core import mail

        for _ in range(3):
            client.post(
                reverse("accounts:password_reset_request"),
                {"email": member.email},
            )
        assert len(mail.outbox) == 3
        # Fourth gets blocked — no new token, no mail
        client.post(
            reverse("accounts:password_reset_request"),
            {"email": member.email},
        )
        assert (
            AccountToken.objects.filter(
                user=member, token_type=AccountToken.TokenType.RESET
            ).count()
            == 3
        )
        assert len(mail.outbox) == 3

    def test_invalidates_previous_unused(self, client, member):
        client.post(
            reverse("accounts:password_reset_request"),
            {"email": member.email},
        )
        client.post(
            reverse("accounts:password_reset_request"),
            {"email": member.email},
        )
        used_count = AccountToken.objects.filter(
            user=member,
            token_type=AccountToken.TokenType.RESET,
            used_at__isnull=False,
        ).count()
        active_count = AccountToken.objects.filter(
            user=member,
            token_type=AccountToken.TokenType.RESET,
            used_at__isnull=True,
        ).count()
        assert used_count == 1
        assert active_count == 1

    def test_emits_password_reset_requested_audit(self, client, member):
        from apps.accounts.models import AccountAuditLog

        client.post(
            reverse("accounts:password_reset_request"),
            {"email": member.email},
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_RESET_REQUESTED,
            target_user=member,
        ).latest("created_at")
        assert entry.actor is None


@pytest.mark.django_db
class TestSetPasswordViewReset:
    """SetPasswordView already implemented in Task 8; verify it accepts RESET tokens."""

    def test_post_with_reset_token_sets_password_and_logs_in(self, client, member):
        from apps.accounts.tokens import issue_token

        raw = issue_token(member, AccountToken.TokenType.RESET)
        resp = client.post(
            reverse("accounts:set_password", kwargs={"token": raw}),
            {"new_password1": "BrandNew99!", "new_password2": "BrandNew99!"},
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.check_password("BrandNew99!")

    def test_audit_message_says_reset(self, client, member):
        from apps.accounts.models import AccountAuditLog
        from apps.accounts.tokens import issue_token

        raw = issue_token(member, AccountToken.TokenType.RESET)
        client.post(
            reverse("accounts:set_password", kwargs={"token": raw}),
            {"new_password1": "BrandNew99!", "new_password2": "BrandNew99!"},
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_SET_FROM_TOKEN,
            target_user=member,
        ).latest("created_at")
        assert "reset" in entry.message


@pytest.mark.django_db
class TestPasswordResetRejectsInactiveUser:
    def test_reset_request_for_inactive_user_returns_generic_success_no_mail(self, client, db):
        from django.core import mail

        user = User.objects.create_user(
            username="OE5BLOCKED",
            email="b@example.org",
            password="x",
            membership_level=User.MembershipLevel.MEMBER,
        )
        user.is_active = False
        user.save()
        resp = client.post(
            reverse("accounts:password_reset_request"),
            {"email": user.email},
        )
        assert resp.status_code == 302
        assert resp.url == reverse("accounts:login")
        # No token issued, no mail sent
        assert not AccountToken.objects.filter(user=user).exists()
        assert len(mail.outbox) == 0
