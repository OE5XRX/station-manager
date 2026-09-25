import pytest
from django.urls import reverse

from apps.module_firmware.models import (
    QUARANTINE_ATTEMPT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleType,
)
from apps.stations.models import StationAuditLog
from tests.conftest import device_auth_headers


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _setup(fm, station, *, uid="U1", slot="slot0"):
    m = Module.objects.create(
        uid=uid, module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
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


def _post(client, priv, station_pk, cid, payload):
    import json

    body = json.dumps(payload).encode()
    return client.post(
        reverse("module_firmware_api:reconcile_status", args=[cid]),
        data=body,
        content_type="application/json",
        **device_auth_headers(priv, station_pk, body),
    )


@pytest.mark.django_db
def test_status_downloading_ok(client, station_with_key, fm):
    station, priv = station_with_key
    _, cs = _setup(fm, station)
    resp = _post(client, priv, station.pk, cs.pk, {"status": "downloading"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert (
        StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.MODULE_FLASH_STARTED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_status_rejected_quarantines_immediately(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    resp = _post(client, priv, station.pk, cs.pk, {"status": "rejected"})
    assert resp.status_code == 200
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    assert cs.attempts == 0
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.QUARANTINED


@pytest.mark.django_db
def test_status_rolled_back_counts_to_quarantine(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    for _ in range(QUARANTINE_ATTEMPT_LIMIT):
        resp = _post(client, priv, station.pk, cs.pk, {"status": "rolled_back"})
        assert resp.status_code in (200, 409)
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED


@pytest.mark.django_db
def test_status_failed_is_transient(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    for _ in range(5):
        resp = _post(client, priv, station.pk, cs.pk, {"status": "failed"})
        assert resp.status_code == 200
    cs.refresh_from_db()
    assert cs.attempts == 0
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING


@pytest.mark.django_db
def test_status_404_unknown_convergence(client, station_with_key, fm):
    station, priv = station_with_key
    resp = _post(client, priv, station.pk, 99999, {"status": "downloading"})
    assert resp.status_code == 404


@pytest.mark.django_db
def test_status_404_when_module_not_assigned_to_station(
    client, station_with_key, fm, station_factory
):
    # Review Focus #5: convergence belongs to a module assigned elsewhere.
    station, priv = station_with_key
    other = station_factory()
    _, cs = _setup(fm, other, uid="OTHER")
    resp = _post(client, priv, station.pk, cs.pk, {"status": "downloading"})
    assert resp.status_code == 404


@pytest.mark.django_db
def test_status_409_when_quarantined(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    cs.state = ModuleFirmwareConvergenceState.State.QUARANTINED
    cs.save(update_fields=["state"])
    resp = _post(client, priv, station.pk, cs.pk, {"status": "rolled_back"})
    assert resp.status_code == 409
