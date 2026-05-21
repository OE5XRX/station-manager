"""Open-redirect protection on the OIDC RP-initiated logout endpoint.

These tests are regression guards on DOT 3.x's built-in
``post_logout_redirect_uri`` validation. The endpoint is mounted at
``/sso/logout/`` (advertised as ``end_session_endpoint`` in our
discovery document).

DOT's ``RPInitiatedLogoutView`` validates the URI by calling
``Application.post_logout_redirect_uri_allowed`` -> ``redirect_to_uri_allowed``,
which performs exact match on (scheme, hostname, port, path) plus a
querystring-subset check. We assert the *security property*, not the
implementation detail, so the tests pass with any correct
implementation but fail if DOT (or a future override) ever loosens
the check into a substring, prefix, or suffix match.
"""

import pytest
from oauth2_provider.models import Application


@pytest.fixture
def app(db):
    """Application with a registered ``post_logout_redirect_uris`` value."""
    return Application.objects.create(
        name="X",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
        post_logout_redirect_uris="https://example.org/goodbye/",
    )


@pytest.mark.django_db
def test_logout_rejects_unregistered_post_logout_redirect_uri(client, app):
    """An unregistered ``post_logout_redirect_uri`` must NEVER cause a
    302 to the attacker's URI.

    Acceptable responses: 400, 401, 403, or a 200 logout page that
    ignores the bad parameter. What we MUST NOT see is a Location
    header pointing at the attacker.
    """
    resp = client.get(
        "/sso/logout/",
        {
            "client_id": app.client_id,
            "post_logout_redirect_uri": "https://attacker.example/steal",
        },
    )
    location = resp.get("Location", "") or ""
    assert "attacker.example" not in location


@pytest.mark.django_db
def test_logout_allows_registered_post_logout_redirect_uri(client, app):
    """The registered URI should be accepted.

    Exact behavior depends on DOT version and whether the user has an
    active session (may render a confirmation page, may redirect
    immediately). We only assert no 5xx and that, if the response is a
    redirect, the target is one of the registered URIs.
    """
    resp = client.get(
        "/sso/logout/",
        {
            "client_id": app.client_id,
            "post_logout_redirect_uri": "https://example.org/goodbye/",
        },
    )
    assert resp.status_code < 500
    if resp.status_code == 302:
        assert "example.org/goodbye/" in resp.get("Location", "")


@pytest.mark.django_db
def test_logout_uri_substring_match_does_not_pass(client, app):
    """Defense against substring-match bugs.

    ``https://example.org.attacker.com`` contains ``example.org`` but
    is not a registered URI; it must be rejected.
    """
    resp = client.get(
        "/sso/logout/",
        {
            "client_id": app.client_id,
            "post_logout_redirect_uri": "https://example.org.attacker.com/cb",
        },
    )
    location = resp.get("Location", "") or ""
    assert "attacker.com" not in location


@pytest.mark.django_db
def test_logout_uri_with_path_suffix_does_not_pass(client, app):
    """Registered URI is ``https://example.org/goodbye/``.

    A request with ``https://example.org/goodbye/extra`` must NOT be
    accepted as a match -- only exact-match per the OIDC RP-initiated
    logout spec.
    """
    resp = client.get(
        "/sso/logout/",
        {
            "client_id": app.client_id,
            "post_logout_redirect_uri": "https://example.org/goodbye/extra",
        },
    )
    location = resp.get("Location", "") or ""
    assert "goodbye/extra" not in location
