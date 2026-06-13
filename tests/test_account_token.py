"""AccountToken model + tokens.py helpers.

Sub-Spec 2a §2 + §2.2.
"""

import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import AccountToken, User


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestAccountTokenModel:
    def test_token_type_choices_present(self):
        assert set(AccountToken.TokenType.values) == {"welcome", "reset", "verify"}

    def test_expiry_constants(self):
        assert AccountToken.EXPIRY[AccountToken.TokenType.WELCOME] == timedelta(days=7)
        assert AccountToken.EXPIRY[AccountToken.TokenType.RESET] == timedelta(hours=24)
        assert AccountToken.EXPIRY[AccountToken.TokenType.VERIFY] == timedelta(hours=24)

    def test_is_active_unused_unexpired(self, member):
        t = AccountToken.objects.create(
            user=member,
            token_type=AccountToken.TokenType.WELCOME,
            secret_hash="x" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assert t.is_active() is True

    def test_is_active_used_returns_false(self, member):
        t = AccountToken.objects.create(
            user=member,
            token_type=AccountToken.TokenType.WELCOME,
            secret_hash="x" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
            used_at=timezone.now(),
        )
        assert t.is_active() is False

    def test_is_active_expired_returns_false(self, member):
        t = AccountToken.objects.create(
            user=member,
            token_type=AccountToken.TokenType.WELCOME,
            secret_hash="x" * 64,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        assert t.is_active() is False


@pytest.mark.django_db
class TestIssueToken:
    def test_issue_returns_raw_and_stores_hash(self, member):
        from apps.accounts.tokens import issue_token

        raw = issue_token(member, AccountToken.TokenType.WELCOME, ip="127.0.0.1")
        assert isinstance(raw, str)
        assert len(raw) > 20
        # raw is NOT in DB; hash is
        assert not AccountToken.objects.filter(secret_hash=raw).exists()
        stored_hash = hashlib.sha256(raw.encode()).hexdigest()
        token = AccountToken.objects.get(secret_hash=stored_hash)
        assert token.user == member
        assert token.token_type == AccountToken.TokenType.WELCOME
        assert token.ip_created == "127.0.0.1"
        assert token.used_at is None

    def test_issue_with_payload(self, member):
        from apps.accounts.tokens import issue_token

        raw = issue_token(
            member,
            AccountToken.TokenType.VERIFY,
            payload={"new_email": "n@example.org"},
        )
        stored_hash = hashlib.sha256(raw.encode()).hexdigest()
        token = AccountToken.objects.get(secret_hash=stored_hash)
        assert token.payload == {"new_email": "n@example.org"}

    def test_issue_sets_expiry_per_type(self, member):
        from apps.accounts.tokens import issue_token

        raw = issue_token(member, AccountToken.TokenType.RESET)
        stored_hash = hashlib.sha256(raw.encode()).hexdigest()
        token = AccountToken.objects.get(secret_hash=stored_hash)
        expected = timezone.now() + AccountToken.EXPIRY[AccountToken.TokenType.RESET]
        # Allow 5s clock skew
        assert abs((token.expires_at - expected).total_seconds()) < 5


@pytest.mark.django_db
class TestConsumeToken:
    def test_consume_returns_token_and_marks_used(self, member):
        from apps.accounts.tokens import consume_token, issue_token

        raw = issue_token(member, AccountToken.TokenType.WELCOME)
        token = consume_token(raw, AccountToken.TokenType.WELCOME)
        assert token is not None
        assert token.user == member
        assert token.used_at is not None

    def test_consume_twice_returns_none_second_call(self, member):
        from apps.accounts.tokens import consume_token, issue_token

        raw = issue_token(member, AccountToken.TokenType.WELCOME)
        assert consume_token(raw, AccountToken.TokenType.WELCOME) is not None
        assert consume_token(raw, AccountToken.TokenType.WELCOME) is None

    def test_consume_with_wrong_type_returns_none(self, member):
        from apps.accounts.tokens import consume_token, issue_token

        raw = issue_token(member, AccountToken.TokenType.WELCOME)
        assert consume_token(raw, AccountToken.TokenType.RESET) is None
        # And the welcome one is still usable
        assert consume_token(raw, AccountToken.TokenType.WELCOME) is not None

    def test_consume_expired_returns_none(self, member):
        from apps.accounts.tokens import consume_token, issue_token

        raw = issue_token(member, AccountToken.TokenType.WELCOME)
        # Manually expire it
        stored_hash = hashlib.sha256(raw.encode()).hexdigest()
        AccountToken.objects.filter(secret_hash=stored_hash).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        assert consume_token(raw, AccountToken.TokenType.WELCOME) is None

    def test_consume_nonexistent_returns_none(self):
        from apps.accounts.tokens import consume_token

        assert consume_token("nonexistent-raw-string", AccountToken.TokenType.WELCOME) is None


@pytest.mark.django_db
class TestInvalidatePendingTokens:
    def test_invalidate_marks_all_unused_of_type(self, member):
        from apps.accounts.tokens import invalidate_pending_tokens, issue_token

        raw1 = issue_token(member, AccountToken.TokenType.WELCOME)
        raw2 = issue_token(member, AccountToken.TokenType.WELCOME)
        # Reset-token of same user MUST NOT be touched
        raw_reset = issue_token(member, AccountToken.TokenType.RESET)

        invalidate_pending_tokens(member, AccountToken.TokenType.WELCOME)

        h1 = hashlib.sha256(raw1.encode()).hexdigest()
        h2 = hashlib.sha256(raw2.encode()).hexdigest()
        h_reset = hashlib.sha256(raw_reset.encode()).hexdigest()
        assert AccountToken.objects.get(secret_hash=h1).used_at is not None
        assert AccountToken.objects.get(secret_hash=h2).used_at is not None
        assert AccountToken.objects.get(secret_hash=h_reset).used_at is None

    def test_invalidate_skips_already_used(self, member):
        from apps.accounts.tokens import (
            consume_token,
            invalidate_pending_tokens,
            issue_token,
        )

        raw1 = issue_token(member, AccountToken.TokenType.WELCOME)
        consume_token(raw1, AccountToken.TokenType.WELCOME)
        original_used_at = AccountToken.objects.get(
            secret_hash=hashlib.sha256(raw1.encode()).hexdigest()
        ).used_at
        invalidate_pending_tokens(member, AccountToken.TokenType.WELCOME)
        # Already-used row's used_at should NOT be overwritten
        post = AccountToken.objects.get(
            secret_hash=hashlib.sha256(raw1.encode()).hexdigest()
        ).used_at
        assert post == original_used_at
