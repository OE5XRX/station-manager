import pytest
from django.urls import reverse

from apps.api.models import PersonalAccessToken


@pytest.mark.django_db
def test_list_requires_login(client):
    resp = client.get(reverse("accounts:api_tokens"))
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_user_sees_only_own_tokens(client, member_user, admin_user):
    mine, _ = PersonalAccessToken.issue(member_user, name="mine")
    PersonalAccessToken.issue(admin_user, name="theirs")
    client.force_login(member_user)
    resp = client.get(reverse("accounts:api_tokens"))
    assert resp.status_code == 200
    assert b"mine" in resp.content
    assert b"theirs" not in resp.content


@pytest.mark.django_db
def test_create_shows_raw_once(client, member_user, monkeypatch):
    # Make token generation deterministic so we can assert the COMPLETE raw
    # value is rendered (not just its 8-char prefix) — the raw is unrecoverable
    # after this response, so showing the whole token is the contract.
    raw_token = "deterministic-raw-token-value-0123456789"
    monkeypatch.setattr("apps.api.models.secrets.token_urlsafe", lambda _nbytes: raw_token)
    client.force_login(member_user)
    resp = client.post(reverse("accounts:api_tokens"), {"name": "ci"})
    assert resp.status_code == 200
    token = PersonalAccessToken.objects.get(user=member_user, name="ci")
    # the full raw token is rendered exactly once, and the stored hash never is
    assert raw_token.encode() in resp.content
    assert token.prefix.encode() in resp.content
    assert token.token_hash.encode() not in resp.content


@pytest.mark.django_db
def test_cannot_revoke_other_users_token(client, member_user, admin_user):
    theirs, _ = PersonalAccessToken.issue(admin_user, name="theirs")
    client.force_login(member_user)
    resp = client.post(reverse("accounts:api_token_revoke", kwargs={"pk": theirs.pk}))
    assert resp.status_code == 404
    theirs.refresh_from_db()
    assert theirs.revoked_at is None


@pytest.mark.django_db
def test_revoke_own_token(client, member_user):
    mine, _ = PersonalAccessToken.issue(member_user, name="mine")
    client.force_login(member_user)
    resp = client.post(reverse("accounts:api_token_revoke", kwargs={"pk": mine.pk}))
    assert resp.status_code in (302, 200)
    mine.refresh_from_db()
    assert mine.revoked_at is not None
