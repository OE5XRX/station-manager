def test_discovery_endpoint_is_reachable_and_advertises_pkce(client):
    """RPs hit /sso/.well-known/openid-configuration to bootstrap.

    The endpoint must respond 200 with a JSON document advertising
    code_challenge_methods_supported=["S256"] — that's how InvenTree
    decides to send a PKCE challenge.

    Note: django-oauth-toolkit's Discovery document omits
    ``grant_types_supported``; per OIDC Discovery 1.0 §3 that means
    RPs must default to ``["authorization_code", "implicit"]``, which
    is exactly what we need for the code-flow. We instead assert that
    ``"code"`` is in ``response_types_supported`` — same guarantee for
    code-flow capability.
    """
    resp = client.get("/sso/.well-known/openid-configuration/")
    assert resp.status_code == 200
    data = resp.json()
    assert "S256" in data["code_challenge_methods_supported"]
    assert "RS256" in data["id_token_signing_alg_values_supported"]
    assert "code" in data["response_types_supported"]
    assert data["authorization_endpoint"].endswith("/sso/authorize/")
    assert data["token_endpoint"].endswith("/sso/token/")
    assert data["userinfo_endpoint"].endswith("/sso/userinfo/")
    assert data["jwks_uri"].endswith("/sso/.well-known/jwks.json")
