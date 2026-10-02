"""Tests for the User read endpoint (Task 9)."""

import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401

FORBIDDEN = {
    "password",
    "last_login",
    "is_staff",
    "is_superuser",
    "is_active",
    "deleted_at",
    "deleted_by",
}


@pytest.mark.django_db
def test_member_sees_only_self(topology):  # noqa: F811
    resp = bearer(topology["station_user"]).get("/api/v1/users/")
    assert resp.status_code == 200
    assert {u["username"] for u in resp.data["results"]} == {"suser"}


@pytest.mark.django_db
def test_admin_sees_all(topology):  # noqa: F811
    resp = bearer(topology["admin"]).get("/api/v1/users/")
    assert resp.status_code == 200
    assert resp.data["count"] >= 5


@pytest.mark.django_db
def test_member_cannot_retrieve_peer_404(topology):  # noqa: F811
    other = topology["admin"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/users/{other}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_no_sensitive_fields(topology):  # noqa: F811
    resp = bearer(topology["admin"]).get(f"/api/v1/users/{topology['admin'].pk}/")
    assert resp.status_code == 200
    assert FORBIDDEN.isdisjoint(resp.data.keys())
    assert {"id", "username", "email", "membership_level", "is_admin"} <= set(resp.data)
