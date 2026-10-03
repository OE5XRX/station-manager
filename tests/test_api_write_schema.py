"""OpenAPI schema coverage for phase-3 write operations.

Asserts that the rendered schema includes all write endpoints introduced in
Tasks 2-11 — POST/PUT/PATCH/DELETE on the writable resources and the custom
ImageRelease actions.  Uses the JSON schema format so assertions can inspect
the parsed paths dict rather than raw YAML text.
"""

import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401

# Write operations added in Phase 3 (path → expected HTTP methods)
EXPECTED_WRITE_OPS = {
    # Task 2: Station CRUD
    "/api/v1/stations/": {"post"},
    "/api/v1/stations/{id}/": {"put", "patch", "delete"},
    # Task 3: Region + StationTag CRUD
    "/api/v1/regions/": {"post"},
    "/api/v1/regions/{id}/": {"put", "patch", "delete"},
    "/api/v1/station-tags/": {"post"},
    "/api/v1/station-tags/{id}/": {"put", "patch", "delete"},
    # Task 4: Station/Region assignment CRUD
    "/api/v1/station-assignments/": {"post"},
    "/api/v1/station-assignments/{id}/": {"put", "patch", "delete"},
    "/api/v1/region-assignments/": {"post"},
    "/api/v1/region-assignments/{id}/": {"put", "patch", "delete"},
    # Task 5: StationLogEntry + StationPhoto CRUD
    "/api/v1/station-log-entries/": {"post"},
    "/api/v1/station-log-entries/{id}/": {"put", "patch", "delete"},
    "/api/v1/station-photos/": {"post"},
    "/api/v1/station-photos/{id}/": {"put", "patch", "delete"},
    # Task 6: RolloutSequence update + RolloutSequenceEntry CRUD
    "/api/v1/rollout-sequences/{id}/": {"put", "patch"},
    "/api/v1/rollout-sequence-entries/": {"post"},
    "/api/v1/rollout-sequence-entries/{id}/": {"put", "patch", "delete"},
    # Task 7: Deployment trigger (create-only)
    "/api/v1/deployments/": {"post"},
    # Task 8: AlertRule CRUD
    "/api/v1/alert-rules/": {"post"},
    "/api/v1/alert-rules/{id}/": {"put", "patch", "delete"},
    # Task 9: ProvisioningJob trigger (create-only)
    "/api/v1/provisioning-jobs/": {"post"},
    # Task 10: User update (update-only)
    "/api/v1/users/{id}/": {"put", "patch"},
    # Task 11: ImageRelease custom actions
    "/api/v1/images/{id}/archive/": {"post"},
    "/api/v1/images/{id}/restore/": {"post"},
    "/api/v1/images/available/": {"get"},
    "/api/v1/images/import/": {"post"},
}


@pytest.mark.django_db
def test_schema_includes_all_write_operations(topology):  # noqa: F811
    """All phase-3 write paths + methods must appear in the rendered schema."""
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    paths = resp.data["paths"]

    failures = []
    for path, expected_methods in EXPECTED_WRITE_OPS.items():
        if path not in paths:
            failures.append(f"PATH MISSING: {path}")
            continue
        actual_methods = {k for k in paths[path].keys() if k != "parameters"}
        missing_methods = expected_methods - actual_methods
        if missing_methods:
            failures.append(f"METHODS MISSING on {path}: {missing_methods}")

    assert not failures, "\n".join(failures)


@pytest.mark.django_db
def test_schema_image_import_uses_correct_request_body(topology):  # noqa: F811
    """images/import/ must use ImageImportInputSerializer (tag/machine/channel/mark_as_latest),
    not the ImageRelease read serializer."""
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    paths = resp.data["paths"]
    assert "/api/v1/images/import/" in paths

    import_op = paths["/api/v1/images/import/"]["post"]
    assert "requestBody" in import_op, "import action must have a request body"

    # The request body schema must reference ImageImportInput, not ImageRelease
    req_body = import_op["requestBody"]
    content = req_body.get("content", {})
    assert content, "requestBody must have content types"

    for ct, schema_info in content.items():
        ref = schema_info.get("schema", {}).get("$ref", "")
        assert "ImageRelease" not in ref, (
            f"import/ requestBody ({ct}) references ImageRelease — "
            "must reference ImageImportInput instead"
        )


@pytest.mark.django_db
def test_schema_archive_restore_have_no_request_body(topology):  # noqa: F811
    """archive/ and restore/ take no request body — spectacular must not infer one."""
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    paths = resp.data["paths"]

    for action_path in ("/api/v1/images/{id}/archive/", "/api/v1/images/{id}/restore/"):
        assert action_path in paths, f"{action_path} missing from schema"
        op = paths[action_path]["post"]
        assert "requestBody" not in op, (
            f"{action_path} must have no requestBody "
            "(it is an idempotent state-change with no input fields)"
        )


@pytest.mark.django_db
def test_schema_deployment_create_uses_deployment_create_serializer(topology):  # noqa: F811
    """POST /deployments/ must reference DeploymentCreate, not the read serializer."""
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    paths = resp.data["paths"]

    assert "/api/v1/deployments/" in paths
    post_op = paths["/api/v1/deployments/"]["post"]
    assert "requestBody" in post_op

    content = post_op["requestBody"].get("content", {})
    assert content
    for ct, schema_info in content.items():
        ref = schema_info.get("schema", {}).get("$ref", "")
        assert "DeploymentCreate" in ref, (
            f"POST /deployments/ requestBody ({ct}) must reference DeploymentCreate, got: {ref}"
        )
