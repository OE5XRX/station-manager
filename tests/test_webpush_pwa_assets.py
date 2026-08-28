import json

import pytest


@pytest.mark.django_db
def test_service_worker_served_at_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r["Content-Type"]
    assert r["Service-Worker-Allowed"] == "/"


@pytest.mark.django_db
def test_manifest_served_at_root(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r["Content-Type"].startswith("application/manifest+json")
    data = json.loads(r.content)
    assert data["display"] == "standalone"
    assert data["start_url"] == "/"
    assert data["icons"]
