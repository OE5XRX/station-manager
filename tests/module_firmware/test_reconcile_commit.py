import json

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
from apps.stations.models import StationAuditLog
from tests.conftest import device_auth_headers


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _setup(fm, station, *, uid="U1"):
    m = Module.objects.create(
        uid=uid, module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot="slot0")
    # A fleet target so reconcile_module can derive the final state from the
    # module's real reported version at commit time (F4).
    ModuleFirmwareTarget.objects.get_or_create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, defaults={"version": "26.09.15-01"}
    )
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    cs = ModuleFirmwareConvergenceState.objects.create(
        module=m, target_release=rel, state=ModuleFirmwareConvergenceState.State.UPDATING
    )
    return m, cs


def _post(client, priv, station_pk, payload):
    body = json.dumps(payload).encode()
    return client.post(
        reverse("module_firmware_api:reconcile_commit"),
        data=body,
        content_type="application/json",
        **device_auth_headers(priv, station_pk, body),
    )


@pytest.mark.django_db
def test_commit_success_on_match(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    # Realistic post-flash state: the heartbeat has already reported the target
    # version, so the derived convergence (F4) lands OK. The commit no longer
    # trusts the payload blindly — it reconciles against the real reported version.
    m.last_reported_version = "26.09.15-01"
    m.save(update_fields=["last_reported_version"])
    resp = _post(client, priv, station.pk, {"convergence_id": cs.pk, "version": "26.09.15-01"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.OK
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.MODULE_FLASH_SUCCESS
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_commit_mismatch_is_rolled_back_409(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    resp = _post(client, priv, station.pk, {"convergence_id": cs.pk, "version": "26.09.99-99"})
    assert resp.status_code == 409
    assert "detail" in resp.json()
    cs.refresh_from_db()
    assert cs.state != ModuleFirmwareConvergenceState.State.OK


@pytest.mark.django_db
def test_commit_404_unknown(client, station_with_key, fm):
    station, priv = station_with_key
    resp = _post(client, priv, station.pk, {"convergence_id": 99999, "version": "x"})
    assert resp.status_code == 404


@pytest.mark.django_db
def test_commit_409_when_quarantined(client, station_with_key, fm):
    # R2-4: a delayed commit for a QUARANTINED row must be rejected (409),
    # never reported as success — symmetric with the status endpoint's guard.
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    cs.state = ModuleFirmwareConvergenceState.State.QUARANTINED
    cs.save(update_fields=["state"])
    resp = _post(client, priv, station.pk, {"convergence_id": cs.pk, "version": "26.09.15-01"})
    assert resp.status_code == 409
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    assert not StationAuditLog.objects.filter(
        module=m, event_type=StationAuditLog.EventType.MODULE_FLASH_SUCCESS
    ).exists()


@pytest.mark.django_db
def test_commit_200_idempotent_when_already_ok(client, station_with_key, fm):
    # R2-4 correction: an already-OK row is NOT quarantine — it means the module
    # already converged (e.g. a heartbeat reported the target before the agent's
    # commit landed). The commit is a legitimate idempotent confirmation: it must
    # return 200 and leave the row OK, not conflate success with a given-up
    # instruction (which would make a good flash look rejected to Teilbereich D).
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    # Module already reports the target version, so reconcile_module keeps it OK.
    m.last_reported_version = "26.09.15-01"
    m.save(update_fields=["last_reported_version"])
    cs.state = ModuleFirmwareConvergenceState.State.OK
    cs.save(update_fields=["state"])
    resp = _post(client, priv, station.pk, {"convergence_id": cs.pk, "version": "26.09.15-01"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.OK
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK


@pytest.mark.django_db
def test_commit_404_when_not_bound_to_station(client, station_with_key, fm, station_factory):
    # Review Focus #5 (commit side).
    station, priv = station_with_key
    other = station_factory()
    m, cs = _setup(fm, other, uid="OTHER")
    resp = _post(client, priv, station.pk, {"convergence_id": cs.pk, "version": "26.09.15-01"})
    assert resp.status_code == 404
