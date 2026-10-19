import pytest
from django.urls import reverse
from django.utils import timezone

from apps.control.models import ControlLock
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from tests.conftest import device_auth_headers


@pytest.fixture
def fm(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    ModuleFirmwareTarget.objects.create(
        module_type=t, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    return t


def _caps(station, backends=("dfu",)):
    station.agent_capabilities = {"module_ota": {"backends": list(backends)}}
    station.save(update_fields=["agent_capabilities"])


def _release(fm, *, sim=False):
    kwargs = {}
    if sim:
        kwargs = {"sim_storage_key": "sk", "sim_sha256": "b" * 64, "sim_size_bytes": 456}
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=123,
        source_repo="r",
        source_tag="t",
        **kwargs,
    )


def _module(fm, station, *, uid="U1", slot="slot0", synthetic=False):
    m = Module.objects.create(
        uid=uid,
        module_type=fm,
        variant="vhf",
        last_reported_version="26.09.10-01",
        uid_source=Module.UidSource.SYNTHETIC if synthetic else Module.UidSource.STM32_UID,
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    return m


def _check(client, station, priv):
    return client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}",
        content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )


@pytest.mark.django_db
def test_check_204_while_lock_held(client, station_with_key, fm, admin_user):
    station, priv = station_with_key
    _caps(station)
    _release(fm)
    _module(fm, station)
    ControlLock.objects.create(
        station=station, scope="station", holder=admin_user, acquired_at=timezone.now()
    )
    assert _check(client, station, priv).status_code == 204


@pytest.mark.django_db
def test_check_returns_instruction_after_release(client, station_with_key, fm, admin_user):
    station, priv = station_with_key
    _caps(station)
    _release(fm)
    _module(fm, station)
    lock = ControlLock.objects.create(
        station=station, scope="station", holder=admin_user, acquired_at=timezone.now()
    )
    assert _check(client, station, priv).status_code == 204
    lock.holder = None
    lock.acquired_at = None
    lock.save()
    resp = _check(client, station, priv)
    assert resp.status_code == 200
    assert resp.json()["module_uid"] == "U1"


@pytest.mark.django_db
def test_check_sim_module_gets_sim_url_and_checksum(client, station_with_key, fm):
    station, priv = station_with_key
    _caps(station, ("sim",))
    rel = _release(fm, sim=True)
    _module(fm, station, synthetic=True)
    resp = _check(client, station, priv)
    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact"] == "sim"
    assert body["download_url"] == (
        reverse("module_firmware_api:download", args=[rel.pk]) + "?artifact=sim"
    )
    assert body["download_url"].endswith("?artifact=sim")
    assert body["checksum_sha256"] == "b" * 64
    assert body["size_bytes"] == 456


@pytest.mark.django_db
def test_check_real_module_gets_dfu(client, station_with_key, fm):
    station, priv = station_with_key
    _caps(station, ("dfu", "sim"))
    rel = _release(fm, sim=True)
    _module(fm, station)
    resp = _check(client, station, priv)
    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact"] == "dfu"
    assert body["download_url"] == reverse("module_firmware_api:download", args=[rel.pk])
    assert body["checksum_sha256"] == "a" * 64
    assert body["size_bytes"] == 123


@pytest.mark.django_db
def test_check_skips_module_whose_backend_missing(client, station_with_key, fm):
    station, priv = station_with_key
    _caps(station, ("sim",))
    _release(fm, sim=True)
    _module(fm, station)  # real HW, needs dfu
    assert _check(client, station, priv).status_code == 204


@pytest.mark.django_db
def test_check_skips_sim_module_without_sim_artifact(client, station_with_key, fm):
    station, priv = station_with_key
    _caps(station, ("dfu", "sim"))
    _release(fm, sim=False)
    _module(fm, station, uid="S", slot="slot0", synthetic=True)
    m = _module(fm, station, uid="R", slot="slot1")
    resp = _check(client, station, priv)
    assert resp.status_code == 200
    assert resp.json()["module_uid"] == m.uid
    assert resp.json()["artifact"] == "dfu"


@pytest.mark.django_db
def test_check_204_without_capabilities(client, station_with_key, fm):
    station, priv = station_with_key
    _release(fm)
    _module(fm, station)
    assert _check(client, station, priv).status_code == 204


@pytest.mark.django_db
def test_check_still_offers_instruction_past_transient_cap(client, station_with_key, fm):
    """The drain cap only ends the lock-out; the agent keeps getting the instruction."""
    from apps.module_firmware.models import DRAIN_TRANSIENT_LIMIT, ModuleFirmwareConvergenceState

    station, priv = station_with_key
    _caps(station)
    rel = _release(fm)
    m = _module(fm, station)
    ModuleFirmwareConvergenceState.objects.create(
        module=m, target_release=rel, transient_failures=DRAIN_TRANSIENT_LIMIT
    )
    resp = _check(client, station, priv)
    assert resp.status_code == 200
    assert resp.json()["module_uid"] == "U1"


def test_executable_artifact_predicate_shared():
    """drain.py and the check view share one executable predicate (S3 deferred minor)."""
    import inspect

    from apps.module_firmware import drain, reconcile_api_views

    assert reconcile_api_views.executable_artifact is drain.executable_artifact
    assert "executable_artifact(" in inspect.getsource(drain.pending_updates)
    assert "executable_artifact(" in inspect.getsource(reconcile_api_views.ReconcileCheckView)
