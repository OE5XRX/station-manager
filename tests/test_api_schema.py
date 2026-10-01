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
