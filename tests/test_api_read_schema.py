import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401

EXPECTED_PATHS = [
    "/api/v1/stations/",
    "/api/v1/regions/",
    "/api/v1/station-tags/",
    "/api/v1/station-assignments/",
    "/api/v1/region-assignments/",
    "/api/v1/rollout-sequences/",
    "/api/v1/rollout-sequence-entries/",
    "/api/v1/deployments/",
    "/api/v1/deployment-results/",
    "/api/v1/alert-rules/",
    "/api/v1/alerts/",
    "/api/v1/provisioning-jobs/",
    "/api/v1/images/",
    "/api/v1/image-import-jobs/",
    "/api/v1/users/",
    "/api/v1/stations/{id}/telemetry/",
    "/api/v1/stations/{id}/inventory/",
]


@pytest.mark.django_db
def test_schema_lists_all_read_resources(topology):  # noqa: F811
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    paths = set(resp.data["paths"].keys())
    missing = [p for p in EXPECTED_PATHS if p not in paths]
    assert not missing, f"missing from schema: {missing}"


@pytest.mark.django_db
def test_schema_has_no_sensitive_component_fields(topology):  # noqa: F811
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    schemas = resp.data["components"]["schemas"]
    # User schema must not expose password/admin flags
    user_props = set(schemas["User"]["properties"].keys())
    assert {"password", "is_staff", "is_superuser"}.isdisjoint(user_props)
    assert {"is_active", "last_login"}.isdisjoint(user_props)
    # ImageRelease must not expose S3 keys
    img_props = set(schemas["ImageRelease"]["properties"].keys())
    assert {"s3_key", "cosign_bundle_s3_key", "rootfs_s3_key"}.isdisjoint(img_props)
