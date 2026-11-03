"""Task 7: "Erneut versuchen" for a failed module update (spec §5.3)."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.module_firmware import services
from apps.module_firmware.drain import pending_updates
from apps.module_firmware.models import (
    DRAIN_TRANSIENT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.update_status import module_update_status
from apps.stations.models import StationAuditLog

State = ModuleFirmwareConvergenceState.State
TARGET = "26.09.15-01"
OLD = "26.09.10-01"
RETRY = StationAuditLog.EventType.MODULE_FLASH_RETRY


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _online(station):
    station.last_seen = timezone.now()
    station.agent_capabilities = {"module_ota": {"backends": ["dfu"]}}
    station.save(update_fields=["last_seen", "agent_capabilities"])
    return station


def _failed(fm, station, *, uid="F1", state=State.FAILED, boot_key="", module_key=""):
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=TARGET,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
        boot_key_hash=boot_key,
    )
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=TARGET
    )
    m = Module.objects.create(
        uid=uid,
        module_type=fm,
        variant="vhf",
        last_reported_version=OLD,
        reported_boot_key=module_key,
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot="slot0")
    rollup = {
        State.FAILED: Module.Convergence.FAILED,
        State.QUARANTINED: Module.Convergence.QUARANTINED,
        State.NOT_UPDATABLE: Module.Convergence.NOT_UPDATABLE,
    }[state]
    Module.objects.filter(pk=m.pk).update(firmware_convergence=rollup)
    m.refresh_from_db()
    cs = ModuleFirmwareConvergenceState.objects.create(
        module=m,
        target_release=rel,
        state=state,
        attempts=2,
        transient_failures=DRAIN_TRANSIENT_LIMIT,
        phase=ModuleFirmwareConvergenceState.Phase.FLASHING,
        phase_at=timezone.now(),
        instructed_at=timezone.now(),
        reason_code=(
            ModuleFirmwareConvergenceState.ReasonCode.TRANSIENT_LIMIT
            if state == State.FAILED
            else ""
        ),
        last_error_mode=ModuleFirmwareConvergenceState.ErrorMode.TRANSIENT,
        last_error_message="usb timeout",
    )
    return m, cs, rel


def test_retry_failed_resets_and_audits(fm, station, staff_user):
    m, cs, rel = _failed(fm, station)
    assert services.retry_failed(m, user=staff_user, ip_address="10.0.0.9") == [TARGET]
    cs.refresh_from_db()
    assert cs.state == State.UPDATING
    assert cs.transient_failures == 0
    assert cs.phase == "" and cs.phase_at is None
    assert cs.reason_code == "" and cs.instructed_at is None
    # History stays: attempts and the last error are kept.
    assert cs.attempts == 2
    assert cs.last_error_message == "usb timeout"
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING
    (log,) = StationAuditLog.objects.filter(event_type=RETRY)
    assert log.user == staff_user
    assert log.ip_address == "10.0.0.9"
    assert log.module == m and log.station == station
    assert log.message == f"Update für Modul {m.uid} wird erneut versucht: {TARGET}."
    assert log.changes == {"retried_targets": [TARGET]}


def test_retry_failed_audit_suffix(fm, station, staff_user):
    m, _cs, _rel = _failed(fm, station)
    services.retry_failed(m, user=staff_user, audit_suffix=" via API token abc")
    (log,) = StationAuditLog.objects.filter(event_type=RETRY)
    assert log.message.endswith(f"{TARGET}. via API token abc")


def test_retry_then_status_is_pending(fm, station, staff_user):
    _online(station)
    m, _cs, _rel = _failed(fm, station)
    assert module_update_status(m).state == "failed"
    services.retry_failed(m, user=staff_user)
    s = module_update_status(m)
    assert s.state == "pending"
    assert s.reason_text == "Update ausstehend"


@pytest.mark.parametrize("state", [State.NOT_UPDATABLE, State.QUARANTINED])
def test_retry_noop_for_not_updatable_and_quarantined(fm, station, staff_user, state):
    kw = {"boot_key": "1" * 16, "module_key": "2" * 16} if state == State.NOT_UPDATABLE else {}
    m, cs, _rel = _failed(fm, station, state=state, **kw)
    assert services.retry_failed(m, user=staff_user) == []
    cs.refresh_from_db()
    assert cs.state == state
    assert cs.transient_failures == DRAIN_TRANSIENT_LIMIT
    assert not StationAuditLog.objects.filter(event_type=RETRY).exists()


def test_retry_noop_without_row(fm, staff_user):
    m = Module.objects.create(uid="LOOSE", module_type=fm, variant="vhf")
    assert services.retry_failed(m, user=staff_user) == []
    assert not StationAuditLog.objects.filter(event_type=RETRY).exists()


def test_retry_noop_when_updating(fm, station, staff_user):
    m, cs, _rel = _failed(fm, station)
    ModuleFirmwareConvergenceState.objects.filter(pk=cs.pk).update(
        state=State.UPDATING, transient_failures=0
    )
    Module.objects.filter(pk=m.pk).update(firmware_convergence=Module.Convergence.UPDATING)
    assert services.retry_failed(m, user=staff_user) == []
    assert not StationAuditLog.objects.filter(event_type=RETRY).exists()


def test_retry_restarts_drain(fm, station, staff_user, django_capture_on_commit_callbacks):
    _online(station)
    m, cs, _rel = _failed(fm, station)
    assert pending_updates(station) == []
    with django_capture_on_commit_callbacks(execute=True):
        services.retry_failed(m, user=staff_user)
    assert pending_updates(station) == [cs]


# ------------------------------------------------------------------- GUI view


def _url(uid):
    return reverse("module_firmware:module_retry_update", args=[uid])


def test_retry_view_member_forbidden(client, member_user, fm, station):
    m, cs, _rel = _failed(fm, station, uid="RM")
    client.force_login(member_user)
    resp = client.post(_url("RM"))
    assert resp.status_code == 403
    cs.refresh_from_db()
    assert cs.state == State.FAILED
    assert not StationAuditLog.objects.filter(event_type=RETRY).exists()


def test_retry_view_get_not_allowed(client, staff_user, fm, station):
    _failed(fm, station, uid="RG")
    client.force_login(staff_user)
    assert client.get(_url("RG")).status_code == 405


def test_retry_view_staff_redirects_to_detail(client, staff_user, fm, station):
    m, cs, _rel = _failed(fm, station, uid="RS")
    client.force_login(staff_user)
    resp = client.post(_url("RS"))
    assert resp.status_code == 302
    assert resp.url == reverse("module_firmware:module_detail", args=["RS"])
    cs.refresh_from_db()
    assert cs.state == State.UPDATING
    (log,) = StationAuditLog.objects.filter(event_type=RETRY)
    assert log.user == staff_user


def test_retry_view_success_message(client, staff_user, fm, station):
    _failed(fm, station, uid="RMSG")
    client.force_login(staff_user)
    resp = client.post(_url("RMSG"), follow=True)
    msgs = [str(x) for x in resp.context["messages"]]
    assert f"Update für RMSG wird erneut versucht ({TARGET})." in msgs


def test_retry_view_noop_message(client, staff_user, fm, station):
    _failed(fm, station, uid="RQ", state=State.QUARANTINED)
    client.force_login(staff_user)
    resp = client.post(_url("RQ"), follow=True)
    msgs = [str(x) for x in resp.context["messages"]]
    assert "RQ ist nicht im Fehlerzustand." in msgs


def test_retry_view_safe_next(client, staff_user, fm, station):
    _failed(fm, station, uid="RN")
    client.force_login(staff_user)
    nxt = reverse("module_firmware:convergence")
    resp = client.post(_url("RN"), {"next": nxt})
    assert resp.status_code == 302 and resp.url == nxt


def test_retry_view_rejects_external_next(client, staff_user, fm, station):
    _failed(fm, station, uid="RX")
    client.force_login(staff_user)
    resp = client.post(_url("RX"), {"next": "https://evil.example/"})
    assert resp.url == reverse("module_firmware:module_detail", args=["RX"])


def test_retry_view_unknown_module_404(client, staff_user, db):
    client.force_login(staff_user)
    assert client.post(_url("NOPE")).status_code == 404
