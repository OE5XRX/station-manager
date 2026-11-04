"""Task 5: lifecycle audit across check → status → commit (spec §5.4, §6).

INSTRUCTED is logged once per open instruction, phases once per change, and the
in-flight bookkeeping (phase / phase_at / instructed_at) is cleared when the
attempt ends."""

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

E = StationAuditLog.EventType
TARGET = "26.09.15-01"


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture(autouse=True)
def _dfu_capable_station(db, station):
    station.agent_capabilities = {"module_ota": {"backends": ["dfu"]}}
    station.save(update_fields=["agent_capabilities"])


@pytest.fixture
def drifted(fm, station_with_key):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=TARGET
    )
    ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=TARGET,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=123,
        source_repo="r",
        source_tag="t",
    )
    m = Module.objects.create(
        uid="U1", module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot="slot0")
    return station, priv, m


def _check(client, station, priv):
    return client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}",
        content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )


def _status(client, station, priv, cid, status, error_message=""):
    payload = {"status": status}
    if error_message:
        payload["error_message"] = error_message
    body = json.dumps(payload).encode()
    return client.post(
        reverse("module_firmware_api:reconcile_status", args=[cid]),
        data=body,
        content_type="application/json",
        **device_auth_headers(priv, station.pk, body),
    )


def _commit(client, station, priv, cid, version):
    body = json.dumps({"convergence_id": cid, "version": version}).encode()
    return client.post(
        reverse("module_firmware_api:reconcile_commit"),
        data=body,
        content_type="application/json",
        **device_auth_headers(priv, station.pk, body),
    )


def _count(module, event_type):
    return StationAuditLog.objects.filter(module=module, event_type=event_type).count()


def _instruct(client, station, priv):
    resp = _check(client, station, priv)
    assert resp.status_code == 200
    return resp.json()["convergence_id"]


@pytest.mark.django_db
def test_check_polling_logs_instructed_once(client, drifted):
    station, priv, m = drifted
    for _ in range(10):
        _instruct(client, station, priv)
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 1
    entry = StationAuditLog.objects.get(module=m, event_type=E.MODULE_FLASH_INSTRUCTED)
    assert entry.station == station
    assert entry.message == f"Modul U1: Update-Anweisung auf {TARGET} ausgegeben (dfu)."
    cs = ModuleFirmwareConvergenceState.objects.get(module=m)
    assert cs.instructed_at is not None


@pytest.mark.django_db
def test_instructed_logged_again_after_failed_status(client, drifted):
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    assert _status(client, station, priv, cid, "failed").status_code == 200
    _instruct(client, station, priv)
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 2


@pytest.mark.django_db
def test_check_after_progress_is_a_restarted_attempt(client, drifted):
    """The agent only polls check with no flash in flight (no marker), so a check
    after a progress POST means the agent crashed/rebooted mid-download: the
    restarted attempt is audited again and the stale phase is cleared (§5.4)."""
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    assert _status(client, station, priv, cid, "downloading").status_code == 200
    _instruct(client, station, priv)  # agent restarted: same instruction again
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 2
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.phase == ""
    assert cs.phase_at is None
    assert cs.instructed_at is not None
    # The restarted download is a fresh first progress POST -> STARTED again.
    assert _status(client, station, priv, cid, "downloading").status_code == 200
    assert _count(m, E.MODULE_FLASH_STARTED) == 2
    assert _count(m, E.MODULE_FLASH_PHASE) == 0


@pytest.mark.django_db
def test_repeated_check_after_restart_logs_once(client, drifted):
    """No status POST between polls -> one INSTRUCTED per attempt, also after a
    restart."""
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    _status(client, station, priv, cid, "flashing")
    for _ in range(5):
        _instruct(client, station, priv)
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 2


@pytest.mark.django_db
def test_phases_logged_once_each(client, drifted):
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    for s in ["downloading", "downloading", "flashing", "verifying", "verifying"]:
        assert _status(client, station, priv, cid, s).status_code == 200
    assert _count(m, E.MODULE_FLASH_STARTED) == 1
    assert _count(m, E.MODULE_FLASH_PHASE) == 2
    messages = list(
        StationAuditLog.objects.filter(module=m, event_type=E.MODULE_FLASH_PHASE)
        .order_by("pk")
        .values_list("message", flat=True)
    )
    assert messages == [
        "Modul U1: Phase downloading → flashing.",
        "Modul U1: Phase flashing → verifying.",
    ]
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.phase == "verifying"
    assert cs.phase_at is not None
    assert cs.last_attempt_at is not None


@pytest.mark.django_db
def test_same_phase_twice_logs_once(client, drifted):
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    _status(client, station, priv, cid, "flashing")
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    first_phase_at = cs.phase_at
    _status(client, station, priv, cid, "flashing")
    _status(client, station, priv, cid, "flashing")
    cs.refresh_from_db()
    assert cs.phase_at == first_phase_at  # no write on a repeated phase
    assert _count(m, E.MODULE_FLASH_STARTED) == 1
    assert _count(m, E.MODULE_FLASH_PHASE) == 0


@pytest.mark.django_db
def test_commit_clears_phase(client, drifted):
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    _status(client, station, priv, cid, "verifying")
    assert _commit(client, station, priv, cid, TARGET).status_code == 200
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.phase == ""
    assert cs.phase_at is None
    assert cs.instructed_at is None


@pytest.mark.django_db
def test_failed_status_clears_phase(client, drifted):
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    _status(client, station, priv, cid, "flashing")
    assert _status(client, station, priv, cid, "failed").status_code == 200
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.phase == ""
    assert cs.phase_at is None
    assert cs.instructed_at is None


@pytest.mark.django_db
def test_new_instruction_clears_stale_phase(client, drifted):
    from django.utils import timezone

    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    # A stale phase left behind by a lost attempt (agent crash, no terminal POST):
    # instructed_at stays set, exactly as a real crash leaves it.
    ModuleFirmwareConvergenceState.objects.filter(pk=cid).update(
        phase="flashing", phase_at=timezone.now()
    )
    _instruct(client, station, priv)
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 2
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.phase == ""
    assert cs.phase_at is None
    assert cs.instructed_at is not None


@pytest.mark.django_db
def test_second_attempt_logs_started_again(client, drifted):
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    _status(client, station, priv, cid, "downloading")
    _status(client, station, priv, cid, "failed")
    _instruct(client, station, priv)
    _status(client, station, priv, cid, "downloading")
    assert _count(m, E.MODULE_FLASH_STARTED) == 2
    assert _count(m, E.MODULE_FLASH_PHASE) == 0


@pytest.mark.django_db
def test_rolled_back_status_audit_carries_key_hint(client, drifted):
    from apps.module_firmware.mcuboot import KEY_UNKNOWN_HINT

    station, priv, m = drifted  # neither module nor release knows a boot key
    cid = _instruct(client, station, priv)
    assert _status(client, station, priv, cid, "rolled_back").status_code == 200
    entry = StationAuditLog.objects.get(module=m, event_type=E.MODULE_FLASH_ROLLED_BACK)
    assert entry.message.endswith(KEY_UNKNOWN_HINT)


@pytest.mark.django_db
def test_rolled_back_status_audit_no_hint_when_keys_known(client, drifted):
    from apps.module_firmware.mcuboot import KEY_UNKNOWN_HINT

    station, priv, m = drifted
    m.reported_boot_key = "0123456789abcdef"
    m.save(update_fields=["reported_boot_key"])
    ModuleFirmwareRelease.objects.update(boot_key_hash="0123456789abcdef")
    cid = _instruct(client, station, priv)
    _status(client, station, priv, cid, "rolled_back")
    entry = StationAuditLog.objects.get(module=m, event_type=E.MODULE_FLASH_ROLLED_BACK)
    assert KEY_UNKNOWN_HINT not in entry.message


@pytest.mark.django_db
def test_failed_status_audit_no_key_hint(client, drifted):
    from apps.module_firmware.mcuboot import KEY_UNKNOWN_HINT

    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    _status(client, station, priv, cid, "failed")
    entry = StationAuditLog.objects.get(module=m, event_type=E.MODULE_FLASH_FAILED)
    assert KEY_UNKNOWN_HINT not in entry.message


@pytest.mark.django_db
def test_commit_mismatch_audit_carries_key_hint(client, drifted):
    from apps.module_firmware.mcuboot import KEY_UNKNOWN_HINT

    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    assert _commit(client, station, priv, cid, "26.09.10-01").status_code == 409
    entry = StationAuditLog.objects.get(module=m, event_type=E.MODULE_FLASH_ROLLED_BACK)
    assert entry.message.endswith(KEY_UNKNOWN_HINT)
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.phase == "" and cs.instructed_at is None


@pytest.mark.django_db
def test_mark_instructed_skips_row_no_longer_updating(drifted):
    """A heartbeat may flip the row (e.g. to not_updatable) between the check's
    reconcile and the mark: the conditional UPDATE then matches nothing — no
    INSTRUCTED audit, no write."""
    from apps.module_firmware.reconcile_api_views import ReconcileCheckView
    from apps.module_firmware.reconciler import reconcile_module

    station, _priv, m = drifted
    cs = reconcile_module(m)
    ModuleFirmwareConvergenceState.objects.filter(pk=cs.pk).update(
        state=ModuleFirmwareConvergenceState.State.NOT_UPDATABLE
    )
    instruction = {"module_uid": m.uid, "target_version": TARGET, "artifact": "dfu"}
    assert ReconcileCheckView._mark_instructed(cs, instruction, station) is False
    cs.refresh_from_db()
    assert cs.instructed_at is None
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 0


@pytest.mark.django_db
def test_mark_instructed_return_contract(drifted):
    """True on the first mark (audited) and on the plain dedup case (still
    updating, already instructed, no phase — no second audit); the check view
    branches on this, so a dedup must never read as 'stale'."""
    from apps.module_firmware.reconcile_api_views import ReconcileCheckView
    from apps.module_firmware.reconciler import reconcile_module

    station, _priv, m = drifted
    cs = reconcile_module(m)
    instruction = {"module_uid": m.uid, "target_version": TARGET, "artifact": "dfu"}
    assert ReconcileCheckView._mark_instructed(cs, instruction, station) is True
    assert ReconcileCheckView._mark_instructed(cs, instruction, station) is True
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "event"),
    [
        ("failed", E.MODULE_FLASH_FAILED),
        ("rolled_back", E.MODULE_FLASH_ROLLED_BACK),
        ("rejected", E.MODULE_FLASH_REJECTED),
    ],
)
def test_failure_status_audit_carries_error_text(client, drifted, status, event):
    """Spec §5.4/§6: failed/rolled_back/rejected are logged with the error text,
    before the key hint (rolled_back/rejected without known keys)."""
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    assert _status(client, station, priv, cid, status, "dfu-util: timeout").status_code == 200
    entry = StationAuditLog.objects.get(module=m, event_type=event)
    assert entry.message.startswith(
        f"Module {m.uid} flash {status} (target {TARGET}). Error: dfu-util: timeout"
    )


@pytest.mark.django_db
def test_failure_status_audit_without_error_text_unchanged(client, drifted):
    station, priv, m = drifted
    cid = _instruct(client, station, priv)
    _status(client, station, priv, cid, "failed")
    entry = StationAuditLog.objects.get(module=m, event_type=E.MODULE_FLASH_FAILED)
    assert entry.message == f"Module {m.uid} flash failed (target {TARGET})."


# --- review round 4: no stale instruction once the row left UPDATING ---------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "new_state",
    [
        ModuleFirmwareConvergenceState.State.NOT_UPDATABLE,
        ModuleFirmwareConvergenceState.State.FAILED,
    ],
)
def test_check_returns_204_when_row_leaves_updating_before_mark(
    client, drifted, monkeypatch, new_state
):
    """A concurrent inventory (not_updatable) or terminal status (failed) flips
    the selected row after reconcile_module returned: check must not hand out
    the stale instruction (the agent does not abort on 409 progress POSTs)."""
    from apps.module_firmware import reconcile_api_views

    station, priv, m = drifted
    real = reconcile_api_views.reconcile_module

    def racing_reconcile(module):
        cs = real(module)
        ModuleFirmwareConvergenceState.objects.filter(pk=cs.pk).update(state=new_state)
        return cs

    monkeypatch.setattr(reconcile_api_views, "reconcile_module", racing_reconcile)
    resp = _check(client, station, priv)
    assert resp.status_code == 204
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 0


@pytest.mark.django_db
def test_dedup_check_still_returns_instruction_without_audit(client, drifted):
    station, priv, m = drifted
    first = _check(client, station, priv)
    second = _check(client, station, priv)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 1
