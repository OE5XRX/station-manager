import pytest
from datetime import timedelta
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from apps.api.authentication import PersonalAccessTokenAuthentication
from apps.api.models import PersonalAccessToken


def _auth(raw):
    req = APIRequestFactory().get("/api/v1/ping/", HTTP_AUTHORIZATION=f"Bearer {raw}")
    return PersonalAccessTokenAuthentication().authenticate(req)


@pytest.mark.django_db
def test_valid_token_authenticates_owner(member_user):
    token, raw = PersonalAccessToken.issue(member_user, name="x")
    user, auth = _auth(raw)
    assert user == member_user
    assert auth == token
    token.refresh_from_db()
    assert token.last_used_at is not None


@pytest.mark.django_db
def test_no_header_returns_none():
    req = APIRequestFactory().get("/api/v1/ping/")
    assert PersonalAccessTokenAuthentication().authenticate(req) is None


@pytest.mark.django_db
def test_malformed_header_is_clean_401_or_none():
    # Non-Bearer scheme -> None (let other authenticators try).
    for other_scheme in ["Token abc", "DeviceKey 1"]:
        req = APIRequestFactory().get("/api/v1/ping/", HTTP_AUTHORIZATION=other_scheme)
        assert PersonalAccessTokenAuthentication().authenticate(req) is None
    # Bearer but malformed -> clean AuthenticationFailed (401), never a 500.
    for bad in ["Bearer", "Bearer   ", "Bearer a b"]:
        req = APIRequestFactory().get("/api/v1/ping/", HTTP_AUTHORIZATION=bad)
        with pytest.raises(AuthenticationFailed):
            PersonalAccessTokenAuthentication().authenticate(req)


@pytest.mark.django_db
def test_last_used_write_failure_does_not_break_auth(member_user, monkeypatch):
    token, raw = PersonalAccessToken.issue(member_user, name="x")

    # The auth lookup uses .select_related(...).get(...); only the
    # last_used_at stamp uses .filter(...).update(...). Make .filter blow
    # up so the stamp path raises — auth must still succeed.
    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(type(PersonalAccessToken.objects), "filter", _boom)
    user, auth = _auth(raw)
    assert user == member_user


@pytest.mark.django_db
def test_unknown_token_rejected():
    with pytest.raises(AuthenticationFailed):
        _auth("totally-bogus-token-value-123456")


@pytest.mark.django_db
def test_expired_and_revoked_rejected(member_user):
    expired, raw_e = PersonalAccessToken.issue(
        member_user, name="e", expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(AuthenticationFailed):
        _auth(raw_e)
    revoked, raw_r = PersonalAccessToken.issue(member_user, name="r")
    revoked.revoked_at = timezone.now()
    revoked.save(update_fields=["revoked_at"])
    with pytest.raises(AuthenticationFailed):
        _auth(raw_r)


@pytest.mark.django_db
def test_bearer_with_invalid_utf8_is_clean_401():
    """Invalid UTF-8/ASCII bytes in the token must produce a clean 401, never a 500.

    We set META directly because APIRequestFactory encodes the header via
    latin-1 and would reject surrogates.  get_authorization_header() returns
    the raw bytes unchanged when META already contains bytes.
    """
    from django.test import RequestFactory as DjangoRequestFactory

    req = DjangoRequestFactory().get("/api/v1/ping/")
    req.META["HTTP_AUTHORIZATION"] = b"Bearer \xff\xfe"
    with pytest.raises(AuthenticationFailed):
        PersonalAccessTokenAuthentication().authenticate(req)


@pytest.mark.django_db
def test_inactive_owner_rejected(member_user):
    token, raw = PersonalAccessToken.issue(member_user, name="x")
    member_user.is_active = False
    member_user.save(update_fields=["is_active"])
    with pytest.raises(AuthenticationFailed):
        _auth(raw)
