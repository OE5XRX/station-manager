import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.api.models import PersonalAccessToken


@pytest.mark.django_db
def test_issue_returns_raw_once_and_stores_only_hash(member_user):
    token, raw = PersonalAccessToken.issue(member_user, name="ci-script")
    assert raw and len(raw) >= 32
    assert token.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in (token.token_hash, token.prefix)
    assert token.prefix == raw[:8]


@pytest.mark.django_db
def test_is_active_true_for_fresh_token(member_user):
    token, _ = PersonalAccessToken.issue(member_user, name="x")
    assert token.is_active() is True


@pytest.mark.django_db
def test_is_active_false_when_expired(member_user):
    token, _ = PersonalAccessToken.issue(
        member_user, name="x", expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert token.is_active() is False


@pytest.mark.django_db
def test_is_active_false_when_revoked(member_user):
    token, _ = PersonalAccessToken.issue(member_user, name="x")
    token.revoked_at = timezone.now()
    assert token.is_active() is False
