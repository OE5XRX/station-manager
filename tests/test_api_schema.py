import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_openapi_schema_is_served(client):
    resp = client.get(reverse("api:schema"))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/vnd.oai.openapi")


@pytest.mark.django_db
def test_swagger_docs_served(client):
    resp = client.get(reverse("api:docs"))
    assert resp.status_code == 200


def test_personal_access_token_bearer_scheme_registered():
    # A drf-spectacular extension must be registered for the custom Bearer
    # authenticator; otherwise generated operations (Phase 2/3) silently omit
    # the bearer security scheme and Swagger's Authorize flow cannot use tokens.
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    from apps.api.authentication import PersonalAccessTokenAuthentication

    ext = OpenApiAuthenticationExtension.get_match(PersonalAccessTokenAuthentication())
    assert ext is not None, (
        "no OpenApiAuthenticationExtension for PersonalAccessTokenAuthentication"
    )
    assert ext.get_security_definition(None) == {"type": "http", "scheme": "bearer"}
