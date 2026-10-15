"""Tests for the User read endpoint (``users.view`` + directory visibility)."""

import pytest
from django.utils import timezone

from tests.test_api_read_fixtures import bearer, session, topology  # noqa: F401

FORBIDDEN = {
    "password",
    "last_login",
    "is_staff",
    "is_superuser",
    "is_active",
    "deleted_at",
    "deleted_by",
    "is_admin",
    "is_internal",
}

PRIVATE = {"address", "phone", "latitude", "longitude", "language", "notify_channel"}


@pytest.mark.django_db
def test_member_sees_directory_without_applicants(topology):  # noqa: F811
    resp = bearer(topology["station_user"]).get("/api/v1/users/")
    assert resp.status_code == 200
    assert {u["username"] for u in resp.data["results"]} == {"admin", "staff", "rmgr", "suser"}


@pytest.mark.django_db
def test_admin_sees_all_non_deleted(topology):  # noqa: F811
    gone = topology["staff"]
    gone.deleted_at = timezone.now()
    gone.save(update_fields=["deleted_at"])
    resp = bearer(topology["admin"]).get("/api/v1/users/")
    assert resp.status_code == 200
    assert {u["username"] for u in resp.data["results"]} == {"admin", "rmgr", "suser", "appl"}


@pytest.mark.django_db
def test_applicant_list_forbidden(topology):  # noqa: F811
    resp = session(topology["applicant"]).get("/api/v1/users/")
    assert resp.status_code == 403
    assert resp.data["required"] == "users.view"


@pytest.mark.django_db
def test_member_retrieves_peer_with_public_fields_only(topology):  # noqa: F811
    resp = bearer(topology["station_user"]).get(f"/api/v1/users/{topology['admin'].pk}/")
    assert resp.status_code == 200
    assert resp.data["username"] == "admin"
    assert {"id", "username", "email", "membership_level", "date_joined_year"} <= set(resp.data)
    assert resp.data["date_joined_year"] == topology["admin"].date_joined.year
    # Peers get the join year only, never the full timestamp.
    assert "date_joined" not in resp.data
    assert PRIVATE.isdisjoint(resp.data.keys())


@pytest.mark.django_db
def test_member_sees_minimal_fields_of_hidden_peer(topology):  # noqa: F811
    peer = topology["staff"]
    peer.is_directory_visible = False
    peer.save(update_fields=["is_directory_visible"])
    resp = bearer(topology["station_user"]).get(f"/api/v1/users/{peer.pk}/")
    assert resp.status_code == 200
    assert set(resp.data) == {"id", "username", "membership_level", "avatar"}


@pytest.mark.django_db
def test_member_sees_own_private_fields(topology):  # noqa: F811
    me = topology["station_user"]
    resp = bearer(me).get(f"/api/v1/users/{me.pk}/")
    assert resp.status_code == 200
    assert PRIVATE <= set(resp.data)
    assert {"date_joined", "date_joined_year"} <= set(resp.data)


@pytest.mark.django_db
def test_member_cannot_retrieve_applicant_404(topology):  # noqa: F811
    other = topology["applicant"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/users/{other}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_no_sensitive_fields(topology):  # noqa: F811
    resp = bearer(topology["admin"]).get(f"/api/v1/users/{topology['admin'].pk}/")
    assert resp.status_code == 200
    assert FORBIDDEN.isdisjoint(resp.data.keys())
    assert {"id", "username", "email", "membership_level"} | PRIVATE <= set(resp.data)


@pytest.mark.django_db
def test_serializer_without_request_fails_closed(topology):  # noqa: F811
    from apps.api.read_serializers import UserSerializer

    assert set(UserSerializer(topology["admin"]).data) == {"id"}
