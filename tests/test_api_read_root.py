"""Tests for the API root view auth/permission policy (Finding 1).

The ScopedAPIRootView must carry the same policy as the viewsets:
  - anon → 401
  - applicant (authenticated, bearer) → 403
  - member/admin → 200
"""

import pytest

from tests.test_api_read_fixtures import anon_client, bearer, topology  # noqa: F401


@pytest.mark.django_db
def test_api_root_anon_is_401(topology):  # noqa: F811
    resp = anon_client().get("/api/v1/")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_api_root_applicant_is_403(topology):  # noqa: F811
    resp = bearer(topology["applicant"]).get("/api/v1/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_api_root_admin_is_200(topology):  # noqa: F811
    resp = bearer(topology["admin"]).get("/api/v1/")
    assert resp.status_code == 200
