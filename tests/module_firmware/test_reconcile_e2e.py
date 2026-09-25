import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.control.registry import apply_inventory
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from tests.conftest import device_auth_headers

User = get_user_model()


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _inventory(uid, version, variant="vhf", slot="slot0"):
    return [
        {
            "slot": slot,
            "modules": [
                {
                    "module": "fm",
                    "identity": {
                        "type": "fm", "model": "SA818", "uid": uid,
                        "version": version, "variant": variant,
                    },
                    "capabilities": [],
                    "state": {},
                }
            ],
        }
    ]


def _post(client, priv, station_pk, name, payload, args=None):
    body = json.dumps(payload).encode()
    return client.post(
        reverse(f"module_firmware_api:{name}", args=args or []),
        data=body, content_type="application/json",
        **device_auth_headers(priv, station_pk, body),
    )


@pytest.mark.django_db
def test_e2e_heartbeat_reconcile_check_status_commit_ok(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=42, source_repo="r", source_tag="t",
    )

    # 1. Agent heartbeat reports the ist (old version) -> reconciler sees drift.
    apply_inventory(station, _inventory("UIDE2E", "26.09.10-01"))
    m = Module.objects.get(uid="UIDE2E")
    assert m.firmware_convergence == Module.Convergence.UPDATING

    # 2. check -> one instruction.
    resp = _post(client, priv, station.pk, "reconcile_check", {})
    assert resp.status_code == 200
    body = resp.json()
    cid = body["convergence_id"]
    assert body["target_version"] == "26.09.15-01"
    assert body["download_url"] == reverse("module_firmware_api:download", args=[rel.pk])

    # 3. status progression.
    for st in ("downloading", "flashing", "verifying"):
        r = _post(client, priv, station.pk, "reconcile_status", {"status": st}, args=[cid])
        assert r.status_code == 200

    # 4. agent's post-flash heartbeat reports the new version.
    apply_inventory(station, _inventory("UIDE2E", "26.09.15-01"))

    # 5. commit -> ok.
    r = _post(
        client, priv, station.pk, "reconcile_commit",
        {"convergence_id": cid, "version": "26.09.15-01"},
    )
    assert r.status_code == 200

    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.state == ModuleFirmwareConvergenceState.State.OK

    # 6. next check -> nothing to do.
    r = _post(client, priv, station.pk, "reconcile_check", {})
    assert r.status_code == 204


@pytest.mark.django_db
def test_e2e_quarantine_path_to_dashboard(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=42, source_repo="r", source_tag="t",
    )
    apply_inventory(station, _inventory("QUID", "26.09.10-01"))
    resp = _post(client, priv, station.pk, "reconcile_check", {})
    cid = resp.json()["convergence_id"]

    # Agent reports a rejected flash (foreign signature) -> immediate quarantine.
    r = _post(client, priv, station.pk, "reconcile_status", {"status": "rejected"}, args=[cid])
    assert r.status_code == 200

    m = Module.objects.get(uid="QUID")
    assert m.firmware_convergence == Module.Convergence.QUARANTINED

    # check no longer hands out this module.
    r = _post(client, priv, station.pk, "reconcile_check", {})
    assert r.status_code == 204

    # Dashboard warning surfaces it.
    client.force_login(User.objects.create_user(username="op", password="x"))
    dash = client.get(reverse("dashboard:index"))
    assert dash.context["module_stats"]["quarantined"] == 1
    assert list(dash.context["quarantined_modules"].values_list("uid", flat=True)) == ["QUID"]
