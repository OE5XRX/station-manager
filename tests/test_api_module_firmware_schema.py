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
    "/api/v1/module-firmware-releases/{id}/archive/": {"post"},
    "/api/v1/module-firmware-releases/{id}/restore/": {"post"},
    "/api/v1/module-firmware-releases/available/": {"get"},
    "/api/v1/module-firmware-releases/import/": {"post"},
    "/api/v1/module-firmware-import-jobs/": {"get"},
    "/api/v1/module-firmware-targets/": {"get", "post"},
    "/api/v1/module-firmware-targets/{id}/": {"get", "put", "patch", "delete"},
}


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
def test_schema_generation_emits_no_module_firmware_warnings():
    _generate()
    stats = drainage.GENERATOR_STATS
    emitted = [*stats._warn_cache, *stats._error_cache]
    # apps/module_firmware/*api_views.py are the pre-existing station-agent
    # APIViews (no serializer by design); only the new /api/v1 views count.
    offending = [m for m in emitted if "module" in m.lower() and "apps/module_firmware/" not in m]
    assert not offending, "\n".join(offending)
