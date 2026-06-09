"""End-to-end Authorization-Code + PKCE flow against the live DOT endpoints.

Uses Django's test Client + PKCE helpers. No mocks - every HTTP
roundtrip goes through the real ASGI dispatch, real DB, real token
issuance with the in-memory RSA key from config/settings/test.py.

Asserts:
- 302 redirect to RP after consent
- access + id + refresh tokens issued
- ID-token contains preferred_username/email/groups
- /sso/userinfo/ returns the same claims
- refresh_token grant yields a fresh access_token
"""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from oauth2_provider.models import Application

from apps.sso.models import AppGrant

User = get_user_model()

REDIRECT_URI = "https://rp.example.org/oidc/callback/"


def _pkce_pair():
    """Return (verifier, challenge) suitable for the PKCE S256 flow.

    Per RFC 7636 the verifier is 43-128 chars of unreserved URL chars
    and the challenge is base64url(SHA256(verifier)) without padding.
    """
    verifier = secrets.token_urlsafe(64)[:64]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def _decode_jwt_payload(jwt: str) -> dict:
    """Decode a JWT payload WITHOUT verifying the signature.

    This helper is intentionally unsigned: the tests in this file
    care about claim *shape* (preferred_username, email, groups, etc.).
    They do NOT verify that the JWT signature matches the JWKS-served
    public key — that's a separate concern not covered by any test in
    this branch (test_sso_keys only validates key generation + file
    permissions, not end-to-end signing). Tracked as a follow-up:
    add a JWKS-roundtrip test that fetches /sso/.well-known/jwks.json
    and asserts the issued ID token verifies against it.
    """
    payload_b64 = jwt.split(".")[1]
    # Re-pad before base64-decoding.
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


@pytest.fixture
def application(db):
    """Confidential auth-code client.

    ``hash_client_secret=False`` is required on DOT 3.2+ because the
    default is to hash the secret on save, making the raw value
    irrecoverable for the token-endpoint Basic-Auth header.
    """
    return Application.objects.create(
        name="InvenTree-Test",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=REDIRECT_URI,
        post_logout_redirect_uris="https://rp.example.org/goodbye/",
        client_secret="test-secret-do-not-hash",
        hash_client_secret=False,
        algorithm=Application.RS256_ALGORITHM,
        skip_authorization=False,
    )


@pytest.fixture
def authorized_user(db, application):
    """Active user with an AppGrant for ``application``.

    Group membership ("operator") feeds into the ``groups`` claim that
    apps/sso/oidc_claims.py emits.
    """
    g, _ = Group.objects.get_or_create(name="operator")
    u = User.objects.create_user(
        username="peterb",
        password="hunter2",
        email="peter@oe5xrx.org",
        first_name="Peter",
        last_name="Buchegger",
    )
    u.groups.add(g)
    AppGrant.objects.create(user=u, application=application)
    return u


def _authorize_get(client, application, challenge, state="xyz"):
    """GET /sso/authorize/ - returns the consent page (200) or a
    redirect (302) depending on DOT's state machine."""
    return client.get(
        "/sso/authorize/",
        {
            "response_type": "code",
            "client_id": application.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": "openid profile email groups",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )


def _consent_post(client, application, challenge, state="xyz"):
    """POST the consent form. Returns the redirect (302) to the RP."""
    return client.post(
        "/sso/authorize/",
        {
            "client_id": application.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid profile email groups",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow": "Authorize",
        },
    )


def _basic_auth_header(application, raw_secret="test-secret-do-not-hash"):
    """Build the ``Authorization: Basic`` header for the token endpoint.

    DOT 3.2 hashes ``client_secret`` on save (default), so we pass the
    raw secret explicitly rather than reading it back from the model.
    """
    creds = f"{application.client_id}:{raw_secret}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


@pytest.mark.django_db
def test_full_auth_code_pkce_flow_yields_id_token_with_groups_claim(
    client,
    application,
    authorized_user,
):
    verifier, challenge = _pkce_pair()
    client.force_login(authorized_user)

    # --- Step 1: GET authorize -> consent page renders -------------------
    resp = _authorize_get(client, application, challenge)
    assert resp.status_code == 200, (
        f"Expected 200 (consent page), got {resp.status_code}: {resp.content[:500]}"
    )

    # --- Step 2: POST consent -> 302 to RP with ?code= --------------------
    resp = _consent_post(client, application, challenge)
    assert resp.status_code == 302, (
        f"Expected 302 redirect, got {resp.status_code}: {resp.content[:500]}"
    )
    assert resp["Location"].startswith(REDIRECT_URI), (
        f"Expected redirect to RP, got: {resp['Location']}"
    )

    qs = parse_qs(urlparse(resp["Location"]).query)
    assert "error" not in qs, (
        f"Authorize returned an error: {qs.get('error')} ({qs.get('error_description')})"
    )
    assert qs["state"] == ["xyz"], "state parameter must round-trip unchanged"
    code = qs["code"][0]

    # --- Step 3: POST /sso/token/ -> exchange code for tokens -------------
    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": application.client_id,
        },
        HTTP_AUTHORIZATION=_basic_auth_header(application),
    )
    assert resp.status_code == 200, f"Token exchange failed: {resp.content[:500]}"
    data = resp.json()
    assert "access_token" in data, data
    assert "id_token" in data, data
    assert "refresh_token" in data, data
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] > 0

    # --- Step 4: Decode ID-token and check claims -------------------------
    payload = _decode_jwt_payload(data["id_token"])
    assert payload["preferred_username"] == "peterb"
    assert payload["email"] == "peter@oe5xrx.org"
    assert payload["email_verified"] is True
    assert payload["name"] == "Peter Buchegger"
    assert payload["aud"] == application.client_id, (
        f"aud mismatch: expected {application.client_id}, got {payload.get('aud')}"
    )
    # Task 2.2: groups claim is synthesized — membership_level ("applicant"
    # by default for create_user) plus tag:<django-group> entries.
    assert "applicant" in payload["groups"]
    assert "tag:operator" in payload["groups"]
    assert payload["iss"], "issuer claim must be non-empty"

    # --- Step 5: GET /sso/userinfo/ with the access token -----------------
    resp = client.get(
        "/sso/userinfo/",
        HTTP_AUTHORIZATION=f"Bearer {data['access_token']}",
    )
    assert resp.status_code == 200, f"UserInfo failed: {resp.content[:500]}"
    info = resp.json()
    assert info["preferred_username"] == "peterb"
    assert info["email"] == "peter@oe5xrx.org"
    # Task 2.2: synthesized groups schema (see ID-token assertion above).
    assert "applicant" in info["groups"]
    assert "tag:operator" in info["groups"]

    # --- Step 6: TokenSession assertions (Task 7.1) -----------------------
    # The save_bearer_token hook in apps/sso/permissions.py creates a
    # TokenSession row for every refresh-token issuance. On the initial
    # auth-code exchange there is no parent session yet (rotation chain
    # starts at None).
    from apps.sso.models import TokenSession

    session = TokenSession.objects.filter(
        user=authorized_user,
        application=application,
    ).first()
    assert session is not None, "TokenSession should be created on token issuance"
    assert session.parent is None, "Initial session has no parent"
    assert session.refresh_token is not None, "RefreshToken FK must be set"
    assert session.refresh_token.token == data["refresh_token"]
    assert session.revoked_at is None, "Initial session must be active"
    # NB: user_agent capture is best-effort observability and intentionally
    # not asserted here. DOT's oauthlib request wraps Django's META dict
    # verbatim, so the UA arrives keyed as "HTTP_USER_AGENT" rather than
    # "User-Agent". The TokenSession row is recorded regardless. See
    # apps/sso/permissions.py::_record_token_session for the capture logic.


@pytest.mark.django_db
def test_refresh_token_flow_yields_new_access_token(
    client,
    application,
    authorized_user,
):
    """After token exchange, the refresh_token grant gives us a fresh
    access_token. Verifies ROTATE_REFRESH_TOKEN doesn't break the
    standard refresh path."""
    verifier, challenge = _pkce_pair()
    client.force_login(authorized_user)

    # Prime the session via GET (some DOT versions stash request data
    # in the session before consent POST).
    _authorize_get(client, application, challenge)
    resp = _consent_post(client, application, challenge)
    assert resp.status_code == 302, f"Consent POST failed: {resp.status_code} {resp.content[:300]}"
    qs = parse_qs(urlparse(resp["Location"]).query)
    assert "code" in qs, f"No code in redirect: {resp['Location']}"
    code = qs["code"][0]

    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": application.client_id,
        },
        HTTP_AUTHORIZATION=_basic_auth_header(application),
    )
    assert resp.status_code == 200, f"Initial token exchange failed: {resp.content[:500]}"
    first_data = resp.json()
    refresh = first_data["refresh_token"]
    first_access = first_data["access_token"]

    # Exchange refresh -> new access.
    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": application.client_id,
        },
        HTTP_AUTHORIZATION=_basic_auth_header(application),
    )
    assert resp.status_code == 200, f"Refresh failed: {resp.content[:500]}"
    new_data = resp.json()
    assert "access_token" in new_data
    assert new_data["access_token"] != first_access, (
        "Refresh must produce a new access_token, not echo the old one"
    )


@pytest.mark.django_db
def test_authorize_without_appgrant_redirects_with_access_denied(client, application):
    """Invariant: a logged-in user without an active AppGrant for the
    target Application must NOT receive an authorization code.

    AppGrantAuthorizationView (wired in commit 3bfac0a) intercepts the
    authorize POST, denies users with no grant, audits the deny event,
    and bounces back to the RP with ``?error=access_denied`` plus the
    original ``state`` echoed unchanged. The redirect_uri is validated
    against the Application's registered URIs before bouncing — an
    unregistered URI yields a 400 instead (open-redirect guard).
    """
    verifier, challenge = _pkce_pair()
    g, _g_created = Group.objects.get_or_create(name="member")
    u = User.objects.create_user(username="ungrant", password="x", email="u@x.test")
    u.groups.add(g)
    client.force_login(u)
    # NB: no AppGrant created for u + application.

    resp = client.post(
        "/sso/authorize/",
        {
            "client_id": application.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid",
            "state": "deny",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow": "Authorize",
        },
    )

    assert resp.status_code == 302
    parsed = urlparse(resp["Location"])
    qs = parse_qs(parsed.query)
    assert qs.get("error") == ["access_denied"]
    assert qs.get("state") == ["deny"]  # state echoed from request
    assert "code" not in qs  # never issue a code on deny


@pytest.mark.django_db
def test_token_exchange_with_wrong_code_verifier_fails(client, application, authorized_user):
    """Hand the token endpoint a code_verifier that does NOT match the
    code_challenge sent during authorize -> expect invalid_grant.
    """
    verifier, challenge = _pkce_pair()
    client.force_login(authorized_user)

    resp = client.post(
        "/sso/authorize/",
        {
            "client_id": application.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid",
            "state": "x",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow": "Authorize",
        },
    )
    assert resp.status_code == 302, (
        f"authorize POST did not redirect; got {resp.status_code}, body={resp.content[:200]!r}"
    )
    code = parse_qs(urlparse(resp["Location"]).query)["code"][0]

    basic = base64.b64encode(
        f"{application.client_id}:{application.client_secret}".encode()
    ).decode()
    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": "this-is-not-the-real-verifier-12345678901234567890",
        },
        HTTP_AUTHORIZATION=f"Basic {basic}",
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


@pytest.mark.django_db
def test_token_exchange_with_unknown_redirect_uri_fails(client, application, authorized_user):
    """Authorize was issued for the registered redirect_uri; token exchange
    claiming a DIFFERENT redirect_uri must be rejected.

    Different DOT/oauthlib versions return invalid_grant vs invalid_request
    - we just assert 4xx and no token.
    """
    verifier, challenge = _pkce_pair()
    client.force_login(authorized_user)

    resp = client.post(
        "/sso/authorize/",
        {
            "client_id": application.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid",
            "state": "x",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow": "Authorize",
        },
    )
    assert resp.status_code == 302
    code = parse_qs(urlparse(resp["Location"]).query)["code"][0]

    basic = base64.b64encode(
        f"{application.client_id}:{application.client_secret}".encode()
    ).decode()
    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://elsewhere.example.org/cb/",
            "code_verifier": verifier,
        },
        HTTP_AUTHORIZATION=f"Basic {basic}",
    )
    assert resp.status_code == 400, f"expected 4xx; got {resp.status_code} {resp.content[:200]!r}"
    assert "access_token" not in (
        resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
    )


@pytest.mark.django_db
def test_refresh_rotation_chains_token_sessions(client, application, authorized_user):
    """After exchanging an auth code, then exchanging the refresh token,
    expect two TokenSessions: parent (ROTATED) and child (active) with the
    parent FK on the child set.

    Production-path validation for the rotation hook in
    apps/sso/permissions.py. The Task 4.1 unit tests mock the request
    object via SimpleNamespace and cannot exercise DOT's actual flow where
    ``request.refresh_token_instance`` is cleared by DOT before our
    save_bearer_token hook runs. This test exercises the fallback that
    walks ``new_access_token.source_refresh_token`` to recover the parent.
    """
    from apps.sso.models import TokenSession

    verifier, challenge = _pkce_pair()
    client.force_login(authorized_user)

    # --- Step 1: Drive happy-path auth-code exchange ----------------------
    _authorize_get(client, application, challenge)
    resp = _consent_post(client, application, challenge)
    assert resp.status_code == 302, f"Consent POST failed: {resp.status_code} {resp.content[:300]}"
    qs = parse_qs(urlparse(resp["Location"]).query)
    assert "code" in qs, f"No code in redirect: {resp['Location']}"
    code = qs["code"][0]

    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": application.client_id,
        },
        HTTP_AUTHORIZATION=_basic_auth_header(application),
    )
    assert resp.status_code == 200, f"Initial token exchange failed: {resp.content[:500]}"
    first_data = resp.json()
    refresh_value = first_data["refresh_token"]

    # Parent session: created by initial issuance, no parent FK.
    parent = TokenSession.objects.get(
        user=authorized_user,
        application=application,
        parent__isnull=True,
    )
    assert parent.refresh_token is not None, "Parent must reference its RefreshToken"
    assert parent.refresh_token.token == refresh_value
    assert parent.revoked_at is None, "Parent should be active before rotation"

    # --- Step 2: Exchange the refresh token for a new pair ----------------
    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_value,
            "client_id": application.client_id,
        },
        HTTP_AUTHORIZATION=_basic_auth_header(application),
    )
    assert resp.status_code == 200, f"Refresh failed: {resp.content!r}"
    new_refresh = resp.json()["refresh_token"]
    assert new_refresh != refresh_value, "Rotation must change the refresh value"

    # --- Step 3: Verify the chain -----------------------------------------
    parent.refresh_from_db()
    assert parent.revoked_at is not None, "Parent should be marked revoked after rotation"
    assert parent.revoke_reason == TokenSession.RevokeReason.ROTATED

    child = TokenSession.objects.get(parent=parent)
    assert child.refresh_token is not None, "Child must reference its RefreshToken"
    assert child.refresh_token.token == new_refresh
    assert child.revoked_at is None, "Child session should be active"
    assert child.user_id == authorized_user.pk
    assert child.application_id == application.pk
