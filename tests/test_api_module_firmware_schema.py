"""OpenAPI schema coverage for the module-firmware API endpoints."""

import pytest
from drf_spectacular import drainage
from drf_spectacular.generators import SchemaGenerator

EXPECTED_OPS = {
    "/api/v1/module-types/": {"get", "post"},
    "/api/v1/module-types/{id}/": {"get", "put", "patch", "delete"},
    "/api/v1/modules/{id}/": {"get", "patch"},
    "/api/v1/modules/{id}/confirm-registration/": {"post"},
    "/api/v1/modules/{id}/release-quarantine/": {"post"},
    "/api/v1/modules/{id}/retry-update/": {"post"},
    "/api/v1/module-firmware-releases/{id}/archive/": {"post"},
    "/api/v1/module-firmware-releases/{id}/restore/": {"post"},
    "/api/v1/module-firmware-releases/available/": {"get"},
    "/api/v1/module-firmware-releases/import/": {"post"},
    "/api/v1/module-firmware-import-jobs/": {"get"},
    "/api/v1/module-firmware-targets/": {"get", "post"},
    "/api/v1/module-firmware-targets/{id}/": {"get", "put", "patch", "delete"},
}

FORBIDDEN_OPS = {
    "/api/v1/modules/": {"post", "put", "patch", "delete"},
    "/api/v1/modules/{id}/": {"post", "put", "delete"},
    "/api/v1/module-firmware-releases/": {"post", "put", "patch", "delete"},
    "/api/v1/module-firmware-releases/{id}/": {"post", "put", "patch", "delete"},
}

# Pre-existing, unrelated generator output (untyped APIViews of the station
# agent endpoints, assignment viewsets, DeploymentSerializer.get_progress).
# Lower these when fixing one; never raise them to admit new noise.
WARNING_BASELINE = 5
ERROR_BASELINE = 11


def _generate():
    drainage.reset_generator_stats()
    return SchemaGenerator().get_schema(public=True)


@pytest.mark.django_db
def test_schema_includes_module_firmware_operations():
    paths = _generate()["paths"]
    failures = []
    for path, expected in EXPECTED_OPS.items():
        if path not in paths:
            failures.append(f"PATH MISSING: {path}")
            continue
        actual = {k for k in paths[path] if k != "parameters"}
        if expected - actual:
            failures.append(f"METHODS MISSING on {path}: {expected - actual}")
    assert not failures, "\n".join(failures)


@pytest.mark.django_db
def test_schema_omits_forbidden_module_firmware_methods():
    paths = _generate()["paths"]
    failures = []
    for path, forbidden in FORBIDDEN_OPS.items():
        assert path in paths, f"PATH MISSING: {path}"
        present = forbidden & set(paths[path])
        if present:
            failures.append(f"FORBIDDEN METHODS on {path}: {present}")
    assert not failures, "\n".join(failures)


@pytest.mark.django_db
def test_schema_generation_emits_no_enum_collision_warning():
    _generate()
    stats = drainage.GENERATOR_STATS
    collisions = [m for m in stats._warn_cache if "enum naming encountered" in m]
    assert not collisions, "\n".join(collisions)


@pytest.mark.django_db
def test_schema_generation_emits_nothing_from_module_firmware_api():
    _generate()
    stats = drainage.GENERATOR_STATS
    emitted = [*stats._warn_cache, *stats._error_cache]
    offending = [m for m in emitted if "apps/api/module_firmware" in m]
    assert not offending, "\n".join(offending)


@pytest.mark.django_db
def test_schema_generation_warning_count_does_not_grow():
    _generate()
    stats = drainage.GENERATOR_STATS
    warnings, errors = list(stats._warn_cache), list(stats._error_cache)
    assert len(warnings) <= WARNING_BASELINE, "\n".join(warnings)
    assert len(errors) <= ERROR_BASELINE, "\n".join(errors)


def _enum_values(schemas, name):
    return set(schemas[name]["enum"])


@pytest.mark.django_db
def test_schema_documents_update_status_enums():
    schemas = _generate()["components"]["schemas"]
    status = schemas["ModuleUpdateStatus"]["properties"]
    for field, enum in (
        ("state", "ModuleUpdateStateEnum"),
        ("phase", "ModuleUpdatePhaseEnum"),
        ("reason_code", "ModuleUpdateReasonEnum"),
    ):
        assert enum in str(status[field]), (field, status[field])
    assert {"not_updatable", "running", "waiting", "failed", "pending"} <= _enum_values(
        schemas, "ModuleUpdateStateEnum"
    )
    assert {"downloading", "flashing", "verifying"} <= _enum_values(
        schemas, "ModuleUpdatePhaseEnum"
    )
    assert {"boot_key_mismatch", "agent_no_module_ota", "transient_limit"} <= _enum_values(
        schemas, "ModuleUpdateReasonEnum"
    )
    module = schemas["Module"]["properties"]
    assert "ModuleUpdateStatus" in str(module["update_status"])
    assert module["reported_boot_key"]["type"] == "string"
    assert "boot_key_hash" in schemas["ModuleFirmwareRelease"]["properties"]


@pytest.mark.django_db
def test_schema_has_retry_update_path():
    paths = _generate()["paths"]
    op = paths["/api/v1/modules/{id}/retry-update/"]["post"]
    assert "ModuleUpdateRetry" in str(op["responses"]["200"])
