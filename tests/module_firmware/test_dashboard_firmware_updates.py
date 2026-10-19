"""Dashboard indicator for module firmware updates (Teilbereich D, finding 6):
stations waiting for a firmware update ("wartet seit …") and DFU-stuck modules."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.module_firmware.drain import DFU_STUCK_PREFIX, dfu_stuck_modules, draining_stations
from apps.module_firmware.models import (
    DRAIN_TRANSIENT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import Station

User = get_user_model()
State = ModuleFirmwareConvergenceState.State
DFU_MSG = "Modul hängt im DFU-Modus — Power-Cycle nötig"


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _station(name, *, online=True, backends=("dfu",)):
    s = Station.objects.create(name=name, callsign=name.upper())
    s.last_seen = timezone.now() if online else timezone.now() - timedelta(hours=1)
    s.agent_capabilities = {"module_ota": {"backends": list(backends)}}
    s.save(update_fields=["last_seen", "agent_capabilities"])
    return s


def _release(fm, version="26.09.15-01"):
    ModuleFirmwareTarget.objects.get_or_create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, defaults={"version": version}
    )
    return ModuleFirmwareRelease.objects.get_or_create(
        module_type=fm,
        variant="vhf",
        version=version,
        defaults={
            "storage_key": "k",
            "sha256": "a" * 64,
            "size_bytes": 123,
            "source_repo": "r",
            "source_tag": "t",
        },
    )[0]


def _module(fm, station, uid, slot="slot0"):
    m = Module.objects.create(
        uid=uid, module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    if station is not None:
        ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    return m


def _row(m, rel, state=State.UPDATING, **kw):
    return ModuleFirmwareConvergenceState.objects.create(
        module=m, target_release=rel, state=state, **kw
    )


def test_dfu_prefix_matches_agent_message():
    from station_agent.module_ota import DFU_STUCK_MSG

    assert DFU_STUCK_MSG.startswith(DFU_STUCK_PREFIX)


def test_draining_stations_lists_only_active_drains(fm):
    rel = _release(fm)
    waiting = _station("alpha")
    _row(_module(fm, waiting, "A1"), rel)
    offline = _station("bravo", online=False)
    _row(_module(fm, offline, "B1"), rel)
    no_backend = _station("charlie", backends=())
    _row(_module(fm, no_backend, "C1"), rel)
    exhausted = _station("delta")
    _row(_module(fm, exhausted, "D1"), rel, transient_failures=DRAIN_TRANSIENT_LIMIT)
    converged = _station("echo")
    _row(_module(fm, converged, "E1"), rel, state=State.OK)

    result = draining_stations()
    assert [s.name for s, _ in result] == ["alpha"]
    station, status = result[0]
    assert status.active and status.since is not None
    assert status.module_uids == ("A1",)


def test_draining_stations_sorted_longest_waiting_first(fm):
    rel = _release(fm)
    newer = _station("alpha")
    _row(_module(fm, newer, "A1"), rel)
    older = _station("bravo")
    row = _row(_module(fm, older, "B1"), rel)
    ModuleFirmwareConvergenceState.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(hours=3)
    )
    assert [s.name for s, _ in draining_stations()] == ["bravo", "alpha"]


def test_draining_stations_query_count_is_bounded(fm, django_assert_max_num_queries):
    rel = _release(fm)
    for i in range(3):
        _station(f"idle{i}")  # no UPDATING rows: never a candidate
    _row(_module(fm, _station("alpha"), "A1"), rel)
    with django_assert_max_num_queries(12):
        assert len(draining_stations()) == 1


def test_dfu_stuck_modules_latest_error_only(fm):
    rel = _release(fm)
    newer_rel = _release(fm, "26.09.20-01")
    s = _station("alpha")
    stuck = _module(fm, s, "S1", slot="slot1")
    _row(stuck, rel, last_error_message=DFU_MSG, last_error_mode="rolled_back")
    # Converged after a power-cycle: no longer stuck.
    healed = _module(fm, s, "H1", slot="slot2")
    _row(healed, rel, state=State.OK, last_error_message=DFU_MSG)
    # A newer row for the same module superseded the stuck one.
    superseded = _module(fm, s, "N1", slot="slot3")
    old = _row(superseded, rel, last_error_message=DFU_MSG)
    ModuleFirmwareConvergenceState.objects.filter(pk=old.pk).update(
        updated_at=timezone.now() - timedelta(hours=1)
    )
    _row(superseded, newer_rel)
    # Other errors are not DFU-stuck.
    _row(_module(fm, s, "O1", slot="slot4"), rel, last_error_message="download failed")
    # Quarantined after DFU-stuck still needs a power-cycle.
    q = _module(fm, None, "Q1")
    _row(q, rel, state=State.QUARANTINED, last_error_message=DFU_MSG)

    entries = dfu_stuck_modules()
    assert sorted(e.module.uid for e in entries) == ["Q1", "S1"]
    by_uid = {e.module.uid: e for e in entries}
    assert by_uid["S1"].station == s
    assert by_uid["S1"].slot == "slot1"
    assert by_uid["Q1"].station is None


def test_dfu_stuck_rereports_spend_no_attempt_and_keep_notice(fm):
    """Agent: the first DFU-stuck counts (rolled_back), each later cycle re-reports a
    transient ``failed`` with the same message -> attempts stay at 1, never quarantined
    by re-reports, the dashboard keeps listing the module."""
    from apps.module_firmware.reconciler import record_error

    rel = _release(fm)
    m = _module(fm, _station("alpha"), "S1", slot="slot1")
    row = _row(m, rel)
    record_error(row, ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK, DFU_MSG)
    for _ in range(4):
        record_error(row, ModuleFirmwareConvergenceState.ErrorMode.TRANSIENT, DFU_MSG)
    row.refresh_from_db()
    assert row.attempts == 1
    assert row.state == State.UPDATING
    assert row.transient_failures == 4
    assert [e.module.uid for e in dfu_stuck_modules()] == ["S1"]


def test_dfu_stuck_modules_capped_newest_first(fm):
    rel = _release(fm)
    s = _station("alpha")
    rows = []
    for i in range(4):
        row = _row(_module(fm, s, f"S{i}", slot=f"slot{i}"), rel, last_error_message=DFU_MSG)
        ModuleFirmwareConvergenceState.objects.filter(pk=row.pk).update(
            updated_at=timezone.now() - timedelta(minutes=10 - i)
        )
        rows.append(row)
    entries = dfu_stuck_modules(limit=2)
    assert [e.module.uid for e in entries] == ["S3", "S2"]


@pytest.mark.django_db
def test_dashboard_renders_waiting_and_dfu_stuck(client, fm):
    rel = _release(fm)
    s = _station("alpha")
    _row(_module(fm, s, "A1"), rel)
    stuck = _module(fm, s, "S1", slot="slot1")
    _row(stuck, _release(fm, "26.09.20-01"), last_error_message=DFU_MSG)
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("dashboard:index"))
    assert resp.status_code == 200
    assert [st.name for st, _ in resp.context["fw_waiting"]] == ["alpha"]
    assert [e.module.uid for e in resp.context["fw_dfu_stuck"]] == ["S1"]
    body = resp.content.decode()
    assert "data-fw-updates" in body
    assert "wartet seit" in body
    assert reverse("stations:station_detail", args=[s.pk]) in body
    assert "S1" in body and "Power-Cycle" in body


@pytest.mark.django_db
def test_dashboard_hides_panel_when_nothing_pending(client):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("dashboard:index"))
    assert resp.status_code == 200
    assert resp.context["fw_waiting"] == []
    assert resp.context["fw_dfu_stuck"] == []
    assert "data-fw-updates" not in resp.content.decode()


@pytest.mark.django_db
def test_dashboard_hides_firmware_panel_without_modules_view(client, fm, monkeypatch):
    """Module UIDs / error text are module data: gated on ``modules.view``, not only
    on ``dashboard.view`` (today both are Applicant, the gate holds if they diverge)."""
    import dataclasses

    from apps.accounts import permissions

    monkeypatch.setitem(
        permissions.AREAS,
        "modules",
        dataclasses.replace(permissions.AREAS["modules"], view=permissions.Level.STAFF),
    )
    rel = _release(fm)
    s = _station("alpha")
    _row(_module(fm, s, "UID-WAIT-7F3"), rel)
    stuck = _module(fm, s, "UID-DFU-9C2", slot="slot1")
    _row(stuck, _release(fm, "26.09.20-01"), last_error_message=DFU_MSG)
    member = User.objects.create_user(username="m", password="x")
    member.membership_level = User.MembershipLevel.MEMBER
    member.save(update_fields=["membership_level"])
    client.force_login(member)
    resp = client.get(reverse("dashboard:index"))
    assert resp.status_code == 200
    assert resp.context["fw_waiting"] == []
    assert resp.context["fw_dfu_stuck"] == []
    body = resp.content.decode()
    assert "data-fw-updates" not in body
    assert "UID-WAIT-7F3" not in body and "UID-DFU-9C2" not in body and "Power-Cycle" not in body

    staff = User.objects.create_user(username="s", password="x")
    staff.membership_level = User.MembershipLevel.STAFF
    staff.save(update_fields=["membership_level"])
    client.force_login(staff)
    body = client.get(reverse("dashboard:index")).content.decode()
    assert "data-fw-updates" in body and "UID-WAIT-7F3" in body and "UID-DFU-9C2" in body


def test_dfu_stuck_notice_cleared_when_module_answers_on_cdc_again(fm):
    """After a power-cycle the module re-enumerates on CDC and reports its identity:
    it is demonstrably out of DFU, so the dashboard notice must disappear even if the
    row stays UPDATING (no new cycle ran yet). Other errors are kept."""
    from apps.module_firmware.ingest import ingest_module

    rel = _release(fm)
    s = _station("alpha")
    m = _module(fm, s, "S1", slot="slot1")
    stuck = _row(m, rel, last_error_message=DFU_MSG, last_error_mode="rolled_back")
    other = _module(fm, s, "O1", slot="slot2")
    other_row = _row(other, rel, last_error_message="download failed")
    assert [e.module.uid for e in dfu_stuck_modules()] == ["S1"]

    ingest_module(
        s,
        "slot1",
        "fm",
        {"uid": "S1", "version": "26.09.10-01", "variant": "vhf"},
        now=timezone.now(),
    )

    assert dfu_stuck_modules() == []
    stuck.refresh_from_db()
    assert stuck.last_error_message == ""
    assert stuck.last_error_mode == "rolled_back"
    other_row.refresh_from_db()
    assert other_row.last_error_message == "download failed"
