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


@pytest.fixture(autouse=True)
def _dfu_capable_station(db, station):
    # Teil D: the check only hands out instructions for artifact kinds the agent
    # reports as module_ota backends. These stations run a dfu-capable agent.
    station.agent_capabilities = {"module_ota": {"backends": ["dfu"]}}
    station.save(update_fields=["agent_capabilities"])


def _inventory(uid, version, variant="vhf", slot="slot0"):
    return [
        {
            "slot": slot,
            "modules": [
                {
                    "module": "fm",
                    "identity": {
                        "type": "fm",
                        "model": "SA818",
                        "uid": uid,
                        "version": version,
                        "variant": variant,
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
        data=body,
        content_type="application/json",
        **device_auth_headers(priv, station_pk, body),
    )


@pytest.mark.django_db
def test_e2e_heartbeat_reconcile_check_status_commit_ok(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=42,
        source_repo="r",
        source_tag="t",
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

    # 4. agent's post-flash heartbeat reports the new version (crash-safe: the
    # commit derives the final state from the real reported version, so the
    # heartbeat must land before the commit for reconcile_module to see OK).
    apply_inventory(station, _inventory("UIDE2E", "26.09.15-01"))

    # 5. commit -> ok. The step-4 heartbeat already converged the row to OK; the
    # commit's terminal guard (R2-4) rejects only QUARANTINED (a given-up
    # instruction), so this OK row is a legitimate idempotent confirmation:
    # payload matches the target -> MODULE_FLASH_SUCCESS audit -> reconcile_module
    # (reported==target) -> stays OK -> 200.
    r = _post(
        client,
        priv,
        station.pk,
        "reconcile_commit",
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
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=42,
        source_repo="r",
        source_tag="t",
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


def _dfu_stuck_setup(station, fm, uid="DFU1", version="26.09.10-01"):
    """Module online in slot0 with a DFU-stuck notice on its convergence row."""
    from station_agent.module_ota import DFU_STUCK_MSG

    apply_inventory(station, _inventory(uid, version))
    module = Module.objects.get(uid=uid)
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
    cs, _ = ModuleFirmwareConvergenceState.objects.update_or_create(
        module=module,
        defaults={
            "target_release": rel,
            "last_error_message": DFU_STUCK_MSG,
            "last_error_mode": "rolled_back",
            "attempts": 1,
        },
    )
    return cs


@pytest.mark.django_db
def test_stale_reemit_while_online_keeps_dfu_stuck_notice(station, fm):
    """Agent debouncer re-emits the stale last-known identity of a module that hangs in
    DFU (still 'online' on the server): that is no proof it left DFU — notice stays."""
    from apps.module_firmware.drain import dfu_stuck_modules

    cs = _dfu_stuck_setup(station, fm)
    # Mid-flash 'updating' entry, then the raw stale re-emit after resume.
    updating = _inventory("DFU1", "26.09.10-01")
    updating[0]["modules"][0]["status"] = "updating"
    apply_inventory(station, updating)
    apply_inventory(station, _inventory("DFU1", "26.09.10-01"))
    cs.refresh_from_db()
    assert cs.last_error_message.startswith("Modul hängt im DFU-Modus")
    assert [e.module.uid for e in dfu_stuck_modules()] == ["DFU1"]


@pytest.mark.django_db
def test_reconnect_after_power_cycle_clears_dfu_stuck_notice(station, fm):
    """Slot dropped (module in DFU, no CDC) -> soft-offline; after a power-cycle the
    module reports again = genuine offline->online transition — notice cleared."""
    from apps.control.models import StationModule
    from apps.module_firmware.drain import dfu_stuck_modules

    cs = _dfu_stuck_setup(station, fm)
    apply_inventory(station, [])  # agent forgot the slot: module goes offline
    assert not StationModule.objects.get(station=station, module_id="fm").online
    cs.refresh_from_db()
    assert cs.last_error_message  # still stuck while offline
    apply_inventory(station, _inventory("DFU1", "26.09.10-01"))  # power-cycled, back
    cs.refresh_from_db()
    assert cs.last_error_message == ""
    assert dfu_stuck_modules() == []


@pytest.mark.django_db
def test_module_back_after_flash_without_offline_clears_dfu_stuck_notice(station, fm):
    """Review #168 r5: DFU_STUCK was a false positive — the module left DFU on its own and
    the first post-resume scan finds it again, without the server ever seeing it offline.
    Sequence as the agent sends it: 'updating' entry -> firmware_update_finished event ->
    inventory with the real identity. Leaving the updating state makes that report a
    genuine came-back, so the notice is cleared (else it sticks on a quarantined row)."""
    from apps.control.models import StationModule
    from apps.control.registry import set_firmware_updating
    from apps.module_firmware.drain import dfu_stuck_modules

    cs = _dfu_stuck_setup(station, fm)
    ModuleFirmwareConvergenceState.objects.filter(pk=cs.pk).update(state="quarantined")
    updating = _inventory("DFU1", "26.09.10-01")
    updating[0]["modules"][0]["status"] = "updating"
    apply_inventory(station, updating)
    set_firmware_updating(station, "slot0", "fm", False)  # firmware_update_finished
    sm = StationModule.objects.get(station=station, module_id="fm")
    assert sm.firmware_updating is False
    assert sm.online is False  # presence unconfirmed until re-reported
    apply_inventory(station, _inventory("DFU1", "26.09.10-01"))
    cs.refresh_from_db()
    assert cs.last_error_message == ""
    assert dfu_stuck_modules() == []
    assert StationModule.objects.get(station=station, module_id="fm").online is True


@pytest.mark.django_db
def test_finished_event_for_non_updating_row_keeps_it_online(station, fm):
    """A stray firmware_update_finished for a row that was not updating is a no-op."""
    from apps.control.models import StationModule
    from apps.control.registry import set_firmware_updating

    apply_inventory(station, _inventory("OK1", "26.09.10-01"))
    set_firmware_updating(station, "slot0", "fm", False)
    sm = StationModule.objects.get(station=station, module_id="fm")
    assert sm.online is True
    assert sm.firmware_updating is False


@pytest.mark.django_db
@pytest.mark.parametrize("seen_updating", [False, True])
def test_pre_flash_failed_cycle_keeps_module_online(station, fm, seen_updating):
    """Review #168 verify: a fast pre-flash failure (started -> download/checksum fails
    -> finished result 'failed') never flashed the module, so it must stay online —
    the agent may never emit an 'updating' entry nor a changed post-resume inventory."""
    from apps.control.models import StationModule
    from apps.control.registry import set_firmware_updating

    apply_inventory(station, _inventory("OK1", "26.09.10-01"))
    set_firmware_updating(station, "slot0", "fm", True)  # firmware_update_started
    if seen_updating:
        updating = _inventory("OK1", "26.09.10-01")
        updating[0]["modules"][0]["status"] = "updating"
        apply_inventory(station, updating)
    set_firmware_updating(station, "slot0", "fm", False, "failed")  # finished
    sm = StationModule.objects.get(station=station, module_id="fm")
    assert sm.firmware_updating is False
    assert sm.online is True


@pytest.mark.django_db
@pytest.mark.parametrize("result", ["success", "rolled_back", "rejected"])
def test_flashed_cycle_finish_soft_offlines_row(station, fm, result):
    """Any result but 'failed' means the flash ran (rolled_back covers DFU_STUCK): the
    row goes soft-offline until the agent's forced post-finish inventory re-reports it."""
    from apps.control.models import StationModule
    from apps.control.registry import set_firmware_updating

    apply_inventory(station, _inventory("OK1", "26.09.10-01"))
    set_firmware_updating(station, "slot0", "fm", True)
    set_firmware_updating(station, "slot0", "fm", False, result)
    sm = StationModule.objects.get(station=station, module_id="fm")
    assert sm.firmware_updating is False
    assert sm.online is False
