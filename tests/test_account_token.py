"""AccountToken model + tokens.py helpers.

Sub-Spec 2a §2 + §2.2.
"""

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
