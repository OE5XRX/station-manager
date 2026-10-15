"""The per-app SSO policy must be evaluated against the client_id the
authorization code is actually issued for.

DOT's ``AuthorizationView.form_valid`` issues the code for the
``client_id``/``redirect_uri`` in the POST body. A gate that only looks at
the GET query (``?client_id=A``) lets a user allowed for app A obtain a
code for app B by tampering with the consent form body. These tests pin:

- the GET/POST mismatch exploit is denied (no code, no Grant row);
- a matching consent POST for an allowed app still issues a code;
- a denied app named only in the POST body is denied;
- defense in depth: the OAuth2 validator refuses to persist a code for a
  user/application pair the policy denies, independent of the view.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application, Grant
from oauthlib.common import Request as OAuthlibRequest
from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

from apps.sso.models import AppGrant
from apps.sso.permissions import SsoOAuth2Validator

User = get_user_model()

URI_A = "https://a.example.org/oidc/callback/"
URI_B = "https://b.example.org/oidc/callback/"


def _challenge():
    verifier = secrets.token_urlsafe(64)[:64]
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )


def _app(name, uri):
    return Application.objects.create(
        name=name,
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=uri,
        client_secret="s",
        hash_client_secret=False,
        algorithm=Application.RS256_ALGORITHM,
        skip_authorization=False,
    )


@pytest.fixture
def app_a(db):
    return _app("App-A", URI_A)


@pytest.fixture
def app_b(db):
    return _app("App-B", URI_B)


@pytest.fixture
def user_a_only(db, app_a):
    """Active user granted for app A, NOT for app B (GRANT_REQUIRED default)."""
    u = User.objects.create_user(username="aonly", password="x", email="a@x.test")
    AppGrant.objects.create(user=u, application=app_a)
    return u


def _body(app, uri, state="st"):
    return {
        "client_id": app.client_id,
        "redirect_uri": uri,
        "response_type": "code",
        "scope": "openid",
        "state": state,
        "code_challenge": _challenge(),
        "code_challenge_method": "S256",
        "allow": "Authorize",
    }


def _codes_in(resp):
    loc = resp.get("Location", "") if resp.status_code in (301, 302) else ""
    return parse_qs(urlparse(loc).query).get("code", [])


@pytest.mark.django_db
def test_get_post_client_id_mismatch_is_denied(client, app_a, app_b, user_a_only):
    client.force_login(user_a_only)
    resp = client.post(
        f"/sso/authorize/?client_id={app_a.client_id}&redirect_uri={URI_A}",
        _body(app_b, URI_B),
    )
    assert resp.status_code == 403
    assert "Location" not in resp
    assert _codes_in(resp) == []
    assert not Grant.objects.exists()


@pytest.fixture
def validator_hook_neutralised(monkeypatch):
    """Strip SsoOAuth2Validator's policy re-check so only the view gate remains."""
    from oauth2_provider.oauth2_validators import OAuth2Validator

    monkeypatch.setattr(
        SsoOAuth2Validator, "save_authorization_code", OAuth2Validator.save_authorization_code
    )


@pytest.mark.django_db
def test_view_gate_alone_denies_get_post_mismatch(
    client, app_a, app_b, user_a_only, validator_hook_neutralised
):
    client.force_login(user_a_only)
    resp = client.post(
        f"/sso/authorize/?client_id={app_a.client_id}&redirect_uri={URI_A}",
        _body(app_b, URI_B),
    )
    assert resp.status_code == 403
    assert "Location" not in resp
    assert not Grant.objects.exists()


@pytest.mark.django_db
def test_view_gate_alone_denies_post_body_client_id(
    client, app_a, app_b, user_a_only, validator_hook_neutralised
):
    client.force_login(user_a_only)
    resp = client.post("/sso/authorize/", _body(app_b, URI_B, state="b"))
    assert _codes_in(resp) == []
    assert resp.status_code != 200
    assert not Grant.objects.exists()


@pytest.mark.django_db
def test_post_body_client_id_alone_is_evaluated(client, app_a, app_b, user_a_only):
    """No GET client_id at all: the POST body's app B must be gated."""
    client.force_login(user_a_only)
    resp = client.post("/sso/authorize/", _body(app_b, URI_B, state="b"))
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp["Location"]).query)
    assert qs.get("error") == ["access_denied"]
    assert qs.get("state") == ["b"]
    assert "code" not in qs
    assert not Grant.objects.exists()


@pytest.mark.django_db
def test_matching_post_for_allowed_app_issues_code(client, app_a, user_a_only):
    client.force_login(user_a_only)
    resp = client.post(
        f"/sso/authorize/?client_id={app_a.client_id}&redirect_uri={URI_A}",
        _body(app_a, URI_A),
    )
    assert resp.status_code == 302
    assert resp["Location"].startswith(URI_A)
    assert len(_codes_in(resp)) == 1
    assert Grant.objects.filter(application=app_a, user=user_a_only).count() == 1


@pytest.mark.django_db
def test_validator_refuses_code_for_denied_application(app_b, user_a_only):
    """Defense in depth: even if a view gate were bypassed, the validator
    hook that persists every authorization code re-checks the policy."""
    request = OAuthlibRequest(URI_B)
    request.user = user_a_only
    request.client = app_b
    request.client_id = app_b.client_id
    request.redirect_uri = URI_B
    request.state = "st"
    request.scopes = ["openid"]
    with pytest.raises(AccessDeniedError):
        SsoOAuth2Validator().save_authorization_code(app_b.client_id, {"code": "c0de"}, request)
    assert not Grant.objects.exists()
