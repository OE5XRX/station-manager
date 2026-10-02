import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_openapi_schema_is_served(client):
    resp = client.get(reverse("api:schema"))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/vnd.oai.openapi")


@pytest.mark.django_db
def test_swagger_docs_served_csp_safe(client):
    # The docs page must work under the repo CSP (script-src 'self'): assets come
    # from the self-hosted sidecar, not jsDelivr, and the split view keeps the
    # initializer in a same-origin script request instead of an inline block.
    docs_url = reverse("api:docs")
    resp = client.get(docs_url)
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "cdn.jsdelivr.net" not in html
    assert "/static/drf_spectacular_sidecar/" in html
    # the split view serves its initializer JS from the same origin
    script_resp = client.get(docs_url, {"script": ""})
    assert script_resp.status_code == 200
    assert "javascript" in script_resp["Content-Type"]


def test_custom_authenticators_have_openapi_schemes():
    # Both custom authenticators need a drf-spectacular extension, else the
    # generated operations silently omit their security scheme and Swagger's
    # Authorize flow / generated clients cannot authenticate.
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    from apps.api.authentication import (
        DeviceKeyAuthentication,
        PersonalAccessTokenAuthentication,
    )

    pat = OpenApiAuthenticationExtension.get_match(PersonalAccessTokenAuthentication())
    assert pat is not None, (
        "no OpenApiAuthenticationExtension for PersonalAccessTokenAuthentication"
    )
    assert pat.get_security_definition(None) == {"type": "http", "scheme": "bearer"}

    device = OpenApiAuthenticationExtension.get_match(DeviceKeyAuthentication())
    assert device is not None, "no OpenApiAuthenticationExtension for DeviceKeyAuthentication"
    definition = device.get_security_definition(None)
    assert definition["type"] == "apiKey"
    assert definition["in"] == "header"
    assert definition["name"] == "Authorization"
