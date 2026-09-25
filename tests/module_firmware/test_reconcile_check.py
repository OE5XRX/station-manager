import pytest
from django.urls import reverse

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from tests.conftest import device_auth_headers


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _release(fm, version="26.09.15-01", variant="vhf"):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm, variant=variant, version=version,
        storage_key="k", sha256="a" * 64, size_bytes=123, source_repo="r", source_tag="t",
    )


def _drifted_module(fm, station, *, uid="U1", slot="slot0", variant="vhf"):
    m = Module.objects.create(
        uid=uid, module_type=fm, variant=variant, last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    return m


@pytest.mark.django_db
def test_check_204_when_nothing_to_do(client, station_with_key, fm):
    station, priv = station_with_key
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )
    assert resp.status_code == 204


@pytest.mark.django_db
def test_check_returns_one_instruction(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = _release(fm)
    m = _drifted_module(fm, station)
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["module_uid"] == m.uid
    assert body["slot"] == "slot0"
    assert body["module_type"] == "fm"
    assert body["variant"] == "vhf"
    assert body["target_version"] == "26.09.15-01"
    assert body["checksum_sha256"] == "a" * 64
    assert body["size_bytes"] == 123
    assert body["download_url"] == reverse(
        "module_firmware_api:download", args=[rel.pk]
    )
    assert body["convergence_id"] == ModuleFirmwareConvergenceState.objects.get(module=m).pk


@pytest.mark.django_db
def test_check_skips_quarantined(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = _release(fm)
    m = _drifted_module(fm, station)
    ModuleFirmwareConvergenceState.objects.create(
        module=m, target_release=rel,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
    )
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )
    assert resp.status_code == 204


@pytest.mark.django_db
def test_check_prefers_midflight_updating(client, station_with_key, fm):
    # Review Focus #4: two drifted modules; the one already updating wins (resume).
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = _release(fm)
    m0 = _drifted_module(fm, station, uid="U0", slot="slot0")
    m1 = _drifted_module(fm, station, uid="U1", slot="slot1")
    # m1 (higher slot, normally after m0) already has an updating row.
    ModuleFirmwareConvergenceState.objects.create(
        module=m1, target_release=rel, state=ModuleFirmwareConvergenceState.State.UPDATING
    )
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )
    assert resp.status_code == 200
    assert resp.json()["module_uid"] == "U1"


@pytest.mark.django_db
def test_check_requires_device_auth(client, fm):
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
    )
    assert resp.status_code in (401, 403)
