"""Read API (/api/v1/) for module firmware resources (area ``modules``)."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from tests.test_api_read_fixtures import session

pytestmark = pytest.mark.django_db

TARGET = "26.09.15-01"

LIST_ROUTES = [
    "api:module-type-list",
    "api:module-list",
    "api:module-firmware-release-list",
    "api:module-firmware-import-job-list",
    "api:module-firmware-target-list",
]


@pytest.fixture
def fm():
    return ModuleType.objects.create(
        key="fm", display_name="FM", firmware_repo="OE5XRX/FW-RemoteStation"
    )


def _release(mtype, version=TARGET, variant="vhf", **kw):
    return ModuleFirmwareRelease.objects.create(
        module_type=mtype,
        variant=variant,
        version=version,
        storage_key=f"secret/{variant}-{version}",
        cosign_bundle_key=f"secret/{variant}-{version}.bundle",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="OE5XRX/FW-RemoteStation",
        source_tag=f"v{version}",
        **kw,
    )


def _module(mtype, station, *, uid="U1", variant="vhf", slot="slot0"):
    m = Module.objects.create(uid=uid, module_type=mtype, variant=variant)
    if station is not None:
        ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    return m


@pytest.mark.parametrize("route", LIST_ROUTES)
@pytest.mark.parametrize(
    "role,expected",
    [
        ("admin", 200),
        ("staff", 200),
        ("region_mgr", 200),
        ("station_user", 200),
        ("applicant", 200),
        ("anon", (401, 403)),
    ],
)
def test_read_matrix(api_topology, bearer, anon_client, route, role, expected):
    url = reverse(route)
    if role == "anon":
        client = anon_client
    elif role == "applicant":
        client = session(api_topology["applicant"])
    else:
        client = bearer(api_topology[role])
    resp = client.get(url)
    if isinstance(expected, tuple):
        assert resp.status_code in expected
    else:
        assert resp.status_code == expected


def test_release_list_hides_archived_unless_include_archived(api_topology, bearer, fm):
    active = _release(fm, version="1.0")
    archived = _release(fm, version="0.9")
    archived.archive()
    client = bearer(api_topology["station_user"])
    url = reverse("api:module-firmware-release-list")
    ids = {r["id"] for r in client.get(url).data["results"]}
    assert ids == {active.id}
    ids = {r["id"] for r in client.get(url, {"include_archived": "true"}).data["results"]}
    assert ids == {active.id, archived.id}


def test_archived_release_retrievable_by_id(api_topology, bearer, fm):
    archived = _release(fm, version="0.9")
    archived.archive()
    client = bearer(api_topology["station_user"])
    resp = client.get(reverse("api:module-firmware-release-detail", args=[archived.id]))
    assert resp.status_code == 200
    assert resp.data["archived_at"] is not None
    assert client.get(reverse("api:module-firmware-release-list")).data["count"] == 0


def test_release_payload_has_no_storage_keys(api_topology, bearer, fm):
    rel = _release(fm, imported_by=api_topology["admin"])
    resp = bearer(api_topology["admin"]).get(
        reverse("api:module-firmware-release-detail", args=[rel.id])
    )
    assert resp.status_code == 200
    for forbidden in ("storage_key", "cosign_bundle_key"):
        assert forbidden not in resp.data
    assert resp.data["module_type_key"] == "fm"
    assert resp.data["imported_by"] == "api_admin"


def test_module_detail_exposes_assignment_target_and_quarantine(api_topology, bearer, fm):
    station = api_topology["station_in"]
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    m = _module(fm, station)
    Module.objects.filter(pk=m.pk).update(firmware_convergence=Module.Convergence.QUARANTINED)
    ModuleFirmwareConvergenceState.objects.create(
        module=m,
        target_release=rel,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
        attempts=3,
        last_error_mode=ModuleFirmwareConvergenceState.ErrorMode.REJECTED,
        last_error_message="signature mismatch",
    )
    client = bearer(api_topology["station_user"])
    data = client.get(reverse("api:module-detail", args=[m.id])).data
    assert data["assignment"]["station"] == station.id
    assert data["assignment"]["station_name"] == station.name
    assert data["assignment"]["slot"] == "slot0"
    assert data["target_version"] == TARGET
    assert data["has_release"] is True
    assert data["quarantine"]["target_version"] == TARGET
    assert data["quarantine"]["attempts"] == 3
    assert data["quarantine"]["last_error_mode"] == "rejected"
    assert data["module_type_key"] == "fm"
    # list uses the same facts (page-level release keys)
    row = client.get(reverse("api:module-list")).data["results"][0]
    assert row["target_version"] == TARGET
    assert row["has_release"] is True
    assert row["quarantine"]["target_version"] == TARGET


def test_module_without_assignment_has_nulls(api_topology, bearer, fm):
    m = _module(fm, None)
    data = bearer(api_topology["admin"]).get(reverse("api:module-detail", args=[m.id])).data
    assert data["assignment"] is None
    assert data["target_version"] is None
    assert data["has_release"] is False
    assert data["quarantine"] is None


def test_module_filter_station_uses_open_assignment(api_topology, bearer, fm):
    s_in, s_out = api_topology["station_in"], api_topology["station_out"]
    current = _module(fm, s_in, uid="CUR")
    moved = _module(fm, s_out, uid="MOVED")
    # MOVED was at s_in before (closed row) — must not match.
    ModuleAssignmentHistory.objects.create(
        module=moved, station=s_in, slot="slot1", to_ts=timezone.now()
    )
    url = reverse("api:module-list")
    data = bearer(api_topology["admin"]).get(url, {"station": s_in.id}).data
    assert [r["uid"] for r in data["results"]] == [current.uid]


def test_target_variant_coverage(api_topology, bearer, fm):
    _release(fm, variant="vhf")
    _release(fm, variant="")
    _release(fm, variant="uhf", version="other")
    t = ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    data = (
        bearer(api_topology["admin"])
        .get(reverse("api:module-firmware-target-detail", args=[t.id]))
        .data
    )
    assert data["variant_coverage"] == sorted(["vhf", "—"])
    row = bearer(api_topology["admin"]).get(reverse("api:module-firmware-target-list")).data
    assert row["results"][0]["variant_coverage"] == sorted(["vhf", "—"])


def test_module_type_counts_and_blockers(api_topology, bearer, fm):
    _release(fm)
    _module(fm, None)
    data = bearer(api_topology["admin"]).get(reverse("api:module-type-detail", args=[fm.id])).data
    assert data["module_count"] == 1
    assert data["release_count"] == 1
    assert data["target_count"] == 0
    assert isinstance(data["import_blockers"], list)
    assert data["import_blockers"]  # release_asset_prefix missing


def test_import_job_payload(api_topology, bearer, fm):
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo="OE5XRX/FW-RemoteStation", tag="v1", requested_by=None
    )
    data = (
        bearer(api_topology["admin"])
        .get(reverse("api:module-firmware-import-job-detail", args=[job.id]))
        .data
    )
    assert data["requested_by"] is None
    assert data["status"] == "pending"
    filtered = bearer(api_topology["admin"]).get(
        reverse("api:module-firmware-import-job-list"), {"status": "failed"}
    )
    assert filtered.data["count"] == 0


@pytest.mark.parametrize(
    "route",
    [
        "api:module-type-detail",
        "api:module-detail",
        "api:module-firmware-release-detail",
        "api:module-firmware-import-job-detail",
        "api:module-firmware-target-detail",
    ],
)
def test_unknown_id_404(api_topology, bearer, route):
    resp = bearer(api_topology["admin"]).get(reverse(route, args=[999999]))
    assert resp.status_code == 404


def test_openapi_types_computed_fields():
    from drf_spectacular.generators import SchemaGenerator

    schemas = SchemaGenerator().get_schema(public=True)["components"]["schemas"]
    module = schemas["Module"]["properties"]
    assert module["has_release"]["type"] == "boolean"
    assert module["target_version"]["type"] == "string"
    assert module["target_version"].get("nullable") is True
    for name in ("assignment", "quarantine"):
        assert "$ref" in str(module[name]), (name, module[name])
    coverage = schemas["ModuleFirmwareTarget"]["properties"]["variant_coverage"]
    assert coverage["type"] == "array"
    assert coverage["items"]["type"] == "string"


def test_module_list_page_size_capped_at_50(api_topology, bearer):
    from apps.module_firmware.models import Module, ModuleType

    mtype = ModuleType.objects.create(key="cap", display_name="Cap")
    Module.objects.bulk_create([Module(uid=f"cap-{i:03d}", module_type=mtype) for i in range(60)])
    r = bearer(api_topology["admin"]).get(reverse("api:module-list"), {"page_size": 200})
    assert r.status_code == 200
    assert len(r.json()["results"]) == 50
