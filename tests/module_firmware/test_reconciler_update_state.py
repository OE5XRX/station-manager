"""Task 4: sticky failed/not_updatable states, boot-key check, transient -> failed,
key-hint text and the pure target resolver (spec §5.2, §5.3, §5.5, §7)."""

import pytest

from apps.module_firmware import mcuboot
from apps.module_firmware.models import (
    DRAIN_TRANSIENT_LIMIT,
    QUARANTINE_ATTEMPT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.reconciler import (
    effective_target,
    reconcile_module,
    record_error,
    resolve_effective_target,
)
from apps.stations.models import RolloutGroup, StationAuditLog

State = ModuleFirmwareConvergenceState.State
Reason = ModuleFirmwareConvergenceState.ReasonCode
Mode = ModuleFirmwareConvergenceState.ErrorMode
Ev = StationAuditLog.EventType

RELEASE_KEY = "1111111111111111"
OTHER_KEY = "2222222222222222"
DEV_TEXT = (
    "Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release (Dev-Key) "
    "– SWD-Reflash an der Bench nötig"
)
OTHER_TEXT = (
    "Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release (anderer Schlüssel) "
    "– SWD-Reflash an der Bench nötig"
)


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _setup(fm, station, *, module_key="", release_key=RELEASE_KEY, synthetic=False, cs_kw=None):
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    m = Module.objects.create(
        uid="U1",
        module_type=fm,
        variant="vhf",
        last_reported_version="26.09.10-01",
        reported_boot_key=module_key,
        uid_source=Module.UidSource.SYNTHETIC if synthetic else Module.UidSource.STM32_UID,
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot="slot0")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
        boot_key_hash=release_key,
    )
    cs = None
    if cs_kw is not None:
        cs = ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel, **cs_kw)
    return m, rel, cs


def _count(m, event):
    return StationAuditLog.objects.filter(module=m, event_type=event).count()


def _msgs(m, event):
    return list(
        StationAuditLog.objects.filter(module=m, event_type=event).values_list(
            "message", flat=True
        )
    )


# --- pure resolver -------------------------------------------------------------------


@pytest.mark.django_db
def test_resolve_effective_target_matches_effective_target(fm, station_factory):
    group = RolloutGroup.objects.create(name="g", slug="g")
    other = RolloutGroup.objects.create(name="o", slug="o")
    st = station_factory(rollout_group=group)
    lone = station_factory()

    fleet = ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="fleet-v"
    )
    targets = lambda: list(ModuleFirmwareTarget.objects.filter(module_type=fm))  # noqa: E731
    assert resolve_effective_target(st, targets()) == fleet == effective_target(st, fm)

    grp = ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.GROUP, version="group-v", group=group
    )
    assert resolve_effective_target(st, targets()) == grp == effective_target(st, fm)
    assert resolve_effective_target(lone, targets()) == fleet == effective_target(lone, fm)

    stn = ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.STATION, version="st-v", station=st
    )
    assert resolve_effective_target(st, targets()) == stn == effective_target(st, fm)

    # Canary gate: fleet target restricted to another group -> None for ``lone``.
    fleet.canary_group = other
    fleet.save()
    assert resolve_effective_target(lone, targets()) is None
    assert effective_target(lone, fm) is None
    assert resolve_effective_target(lone, []) is None


@pytest.mark.django_db
def test_effective_target_single_query(fm, station, django_assert_num_queries):
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="fleet-v"
    )
    with django_assert_num_queries(1):
        assert effective_target(station, fm).version == "fleet-v"


@pytest.mark.django_db
def test_effective_target_skips_other_stations_targets(
    fm, station_factory, django_assert_num_queries, monkeypatch
):
    """Hot path: only fleet / own-station / own-group targets are loaded."""
    group = RolloutGroup.objects.create(name="g", slug="g")
    other_group = RolloutGroup.objects.create(name="o", slug="o")
    st = station_factory(rollout_group=group)
    lone = station_factory()
    other = station_factory()
    fleet = ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="fleet-v"
    )
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.STATION, version="o-v", station=other
    )
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.GROUP, version="og-v", group=other_group
    )
    grp = ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.GROUP, version="g-v", group=group
    )
    from apps.module_firmware import reconciler

    loaded = []
    real = reconciler.resolve_effective_target

    def spy(station, targets):
        targets = list(targets)
        loaded.extend(targets)
        return real(station, targets)

    monkeypatch.setattr(reconciler, "resolve_effective_target", spy)
    with django_assert_num_queries(1):
        assert effective_target(lone, fm) == fleet
    assert {t.version for t in loaded} == {"fleet-v"}
    loaded.clear()
    assert effective_target(st, fm) == grp
    assert {t.version for t in loaded} == {"fleet-v", "g-v"}


# --- transient -> failed ---------------------------------------------------------------


@pytest.mark.django_db
def test_transient_limit_sets_failed_and_audits_once(fm, station):
    m, _rel, cs = _setup(fm, station, cs_kw={})
    for _ in range(DRAIN_TRANSIENT_LIMIT):
        record_error(cs, Mode.TRANSIENT, "boom")
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == State.FAILED
    assert cs.reason_code == Reason.TRANSIENT_LIMIT
    assert m.firmware_convergence == Module.Convergence.FAILED
    assert _msgs(m, Ev.MODULE_FLASH_GAVE_UP) == [
        f"Modul U1: Update nach {DRAIN_TRANSIENT_LIMIT} Fehlversuchen in Folge abgebrochen "
        "(Ziel 26.09.15-01). Letzte Meldung: boom"
    ]
    log = StationAuditLog.objects.get(module=m, event_type=Ev.MODULE_FLASH_GAVE_UP)
    assert log.station_id == station.pk


@pytest.mark.django_db
def test_gave_up_without_message_omits_last_message(fm, station):
    m, _rel, cs = _setup(fm, station, cs_kw={})
    for _ in range(DRAIN_TRANSIENT_LIMIT):
        record_error(cs, Mode.TRANSIENT)
    assert _msgs(m, Ev.MODULE_FLASH_GAVE_UP) == [
        f"Modul U1: Update nach {DRAIN_TRANSIENT_LIMIT} Fehlversuchen in Folge abgebrochen "
        "(Ziel 26.09.15-01)."
    ]


@pytest.mark.django_db
def test_record_error_audits_use_explicit_station(fm, station, station_factory):
    other = station_factory()
    m, _rel, cs = _setup(fm, station, cs_kw={})
    record_error(cs, Mode.REJECTED, "x", station=other)
    log = StationAuditLog.objects.get(module=m, event_type=Ev.MODULE_QUARANTINED)
    assert log.station_id == other.pk


@pytest.mark.django_db
def test_failed_keeps_last_error_message(fm, station):
    _m, _rel, cs = _setup(fm, station, cs_kw={})
    for i in range(DRAIN_TRANSIENT_LIMIT):
        record_error(cs, Mode.TRANSIENT, f"err {i}")
    cs.refresh_from_db()
    assert cs.state == State.FAILED
    assert cs.last_error_message == f"err {DRAIN_TRANSIENT_LIMIT - 1}"


@pytest.mark.django_db
def test_four_transients_then_verifying_reset_never_fails(fm, station):
    m, _rel, cs = _setup(fm, station, cs_kw={})
    for _ in range(DRAIN_TRANSIENT_LIMIT - 1):
        record_error(cs, Mode.TRANSIENT, "x")
    # ``verifying`` progress resets the consecutive counter (status view).
    cs.transient_failures = 0
    cs.save(update_fields=["transient_failures"])
    for _ in range(DRAIN_TRANSIENT_LIMIT - 1):
        record_error(cs, Mode.TRANSIENT, "x")
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == State.UPDATING
    assert m.firmware_convergence == Module.Convergence.UPDATING
    assert _count(m, Ev.MODULE_FLASH_GAVE_UP) == 0


@pytest.mark.django_db
def test_reconcile_promotes_updating_row_at_limit_to_failed(fm, station):
    from django.utils import timezone

    now = timezone.now()
    m, _rel, cs = _setup(
        fm,
        station,
        cs_kw={
            "transient_failures": DRAIN_TRANSIENT_LIMIT,
            "phase": ModuleFirmwareConvergenceState.Phase.FLASHING,
            "phase_at": now,
            "instructed_at": now,
        },
    )
    out = reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert out.pk == cs.pk
    assert cs.state == State.FAILED
    assert cs.phase == ""
    assert cs.phase_at is None
    assert cs.instructed_at is None
    log = StationAuditLog.objects.get(module=m, event_type=Ev.MODULE_FLASH_GAVE_UP)
    assert log.station_id == station.pk
    assert cs.reason_code == Reason.TRANSIENT_LIMIT
    assert m.firmware_convergence == Module.Convergence.FAILED
    assert _count(m, Ev.MODULE_FLASH_GAVE_UP) == 1
    reconcile_module(m)
    assert _count(m, Ev.MODULE_FLASH_GAVE_UP) == 1


@pytest.mark.django_db
def test_failed_is_sticky_across_reconcile(fm, station):
    m, _rel, cs = _setup(
        fm, station, cs_kw={"state": State.FAILED, "reason_code": Reason.TRANSIENT_LIMIT}
    )
    for _ in range(3):
        out = reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert out.state == State.FAILED
    assert cs.state == State.FAILED
    assert m.firmware_convergence == Module.Convergence.FAILED


@pytest.mark.django_db
def test_failed_becomes_ok_when_version_reached(fm, station):
    m, _rel, cs = _setup(
        fm,
        station,
        cs_kw={
            "state": State.FAILED,
            "reason_code": Reason.TRANSIENT_LIMIT,
            "transient_failures": DRAIN_TRANSIENT_LIMIT,
            "phase": ModuleFirmwareConvergenceState.Phase.FLASHING,
        },
    )
    m.last_reported_version = "26.09.15-01"
    m.save()
    reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == State.OK
    assert cs.transient_failures == 0
    assert cs.reason_code == ""
    assert cs.phase == ""
    assert cs.phase_at is None
    assert cs.instructed_at is None
    assert m.firmware_convergence == Module.Convergence.OK


# --- boot-key check -------------------------------------------------------------------


@pytest.mark.django_db
def test_dev_key_mismatch_sets_not_updatable_with_dev_text(fm, station):
    m, _rel, _cs = _setup(fm, station, module_key=mcuboot.DEV_KEY_HASH_PREFIX)
    cs = reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == State.NOT_UPDATABLE
    assert cs.reason_code == Reason.BOOT_KEY_MISMATCH
    assert m.firmware_convergence == Module.Convergence.NOT_UPDATABLE
    assert _msgs(m, Ev.MODULE_NOT_UPDATABLE) == [DEV_TEXT]
    assert mcuboot.not_updatable_text(m) == DEV_TEXT
    log = StationAuditLog.objects.get(module=m, event_type=Ev.MODULE_NOT_UPDATABLE)
    assert log.station_id == station.pk


@pytest.mark.django_db
def test_other_key_mismatch_text(fm, station):
    m, _rel, _cs = _setup(fm, station, module_key=OTHER_KEY)
    reconcile_module(m)
    assert _msgs(m, Ev.MODULE_NOT_UPDATABLE) == [OTHER_TEXT]
    assert mcuboot.not_updatable_text(m) == OTHER_TEXT


@pytest.mark.django_db
def test_mismatch_audited_once_over_many_reconciles(fm, station):
    m, _rel, _cs = _setup(fm, station, module_key=OTHER_KEY)
    for _ in range(20):
        reconcile_module(m)
    assert _count(m, Ev.MODULE_NOT_UPDATABLE) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("start", [State.UPDATING, State.FAILED, State.QUARANTINED])
def test_mismatch_overrides_failed_and_quarantined(fm, station, start):
    m, _rel, cs = _setup(
        fm,
        station,
        module_key=OTHER_KEY,
        cs_kw={"state": start, "phase": ModuleFirmwareConvergenceState.Phase.DOWNLOADING},
    )
    reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == State.NOT_UPDATABLE
    assert cs.reason_code == Reason.BOOT_KEY_MISMATCH
    assert cs.phase == ""
    assert m.firmware_convergence == Module.Convergence.NOT_UPDATABLE
    assert _count(m, Ev.MODULE_NOT_UPDATABLE) == 1


@pytest.mark.django_db
def test_not_updatable_clears_instructed_at(fm, station):
    """-> NOT_UPDATABLE ends the open instruction, so after UPDATABLE_AGAIN the
    first new instruction is audited again."""
    from django.utils import timezone

    m, _rel, cs = _setup(
        fm,
        station,
        module_key=OTHER_KEY,
        cs_kw={"state": State.UPDATING, "instructed_at": timezone.now()},
    )
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == State.NOT_UPDATABLE
    assert cs.instructed_at is None


@pytest.mark.django_db
def test_quarantined_on_desired_version_with_other_key_is_not_not_updatable(fm, station):
    """A same-version build (dev bench) already reports the desired version: no
    flash is pending, so a key mismatch must not turn it into NOT_UPDATABLE."""
    m, _rel, cs = _setup(fm, station, module_key=OTHER_KEY, cs_kw={"state": State.QUARANTINED})
    m.last_reported_version = "26.09.15-01"
    m.save(update_fields=["last_reported_version"])
    reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == State.QUARANTINED
    assert m.firmware_convergence == Module.Convergence.QUARANTINED
    assert _count(m, Ev.MODULE_NOT_UPDATABLE) == 0


@pytest.mark.django_db
def test_keys_equal_again_releases_only_not_updatable(fm, station):
    m, _rel, cs = _setup(
        fm,
        station,
        module_key=RELEASE_KEY,
        cs_kw={
            "state": State.NOT_UPDATABLE,
            "reason_code": Reason.BOOT_KEY_MISMATCH,
            "attempts": 2,
            "transient_failures": 3,
        },
    )
    out = reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert out.state == State.UPDATING
    assert cs.state == State.UPDATING
    assert cs.attempts == 0
    assert cs.transient_failures == 0
    assert cs.reason_code == ""
    assert m.firmware_convergence == Module.Convergence.UPDATING
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 1
    reconcile_module(m)
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("start", [State.FAILED, State.QUARANTINED])
def test_keys_equal_does_not_release_quarantine_or_failed(fm, station, start):
    m, _rel, cs = _setup(fm, station, module_key=RELEASE_KEY, cs_kw={"state": start})
    reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == start
    assert m.firmware_convergence == start.value
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 0


@pytest.mark.django_db
def test_not_updatable_stays_when_module_stops_reporting_key(fm, station):
    m, _rel, cs = _setup(
        fm,
        station,
        module_key="",
        cs_kw={"state": State.NOT_UPDATABLE, "reason_code": Reason.BOOT_KEY_MISMATCH},
    )
    reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == State.NOT_UPDATABLE
    assert m.firmware_convergence == Module.Convergence.NOT_UPDATABLE
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 0


@pytest.mark.django_db
def test_synthetic_module_skips_key_check(fm, station):
    m, _rel, _cs = _setup(fm, station, module_key=OTHER_KEY, synthetic=True)
    cs = reconcile_module(m)
    assert cs.state == State.UPDATING
    assert _count(m, Ev.MODULE_NOT_UPDATABLE) == 0


@pytest.mark.django_db
@pytest.mark.parametrize("module_key,release_key", [("", RELEASE_KEY), (OTHER_KEY, "")])
def test_missing_key_on_either_side_keeps_old_behaviour(fm, station, module_key, release_key):
    m, _rel, _cs = _setup(fm, station, module_key=module_key, release_key=release_key)
    cs = reconcile_module(m)
    assert cs.state == State.UPDATING
    assert _count(m, Ev.MODULE_NOT_UPDATABLE) == 0


# --- quarantine key hint (§5.5) ---------------------------------------------------------


@pytest.mark.django_db
def test_quarantine_after_rolled_back_with_unknown_key_has_hint(fm, station):
    m, _rel, cs = _setup(fm, station, module_key="", cs_kw={})
    for _ in range(QUARANTINE_ATTEMPT_LIMIT):
        record_error(cs, Mode.ROLLED_BACK, "rb")
    msgs = _msgs(m, Ev.MODULE_QUARANTINED)
    assert len(msgs) == 1
    assert msgs[0].endswith(" " + mcuboot.KEY_UNKNOWN_HINT)
    log = StationAuditLog.objects.get(module=m, event_type=Ev.MODULE_QUARANTINED)
    assert log.station_id == station.pk


@pytest.mark.django_db
def test_quarantine_after_rolled_back_with_known_keys_has_no_hint(fm, station):
    m, _rel, cs = _setup(fm, station, module_key=RELEASE_KEY, cs_kw={})
    for _ in range(QUARANTINE_ATTEMPT_LIMIT):
        record_error(cs, Mode.ROLLED_BACK, "rb")
    msgs = _msgs(m, Ev.MODULE_QUARANTINED)
    assert len(msgs) == 1
    assert mcuboot.KEY_UNKNOWN_HINT not in msgs[0]


@pytest.mark.django_db
def test_quarantine_after_rejected_has_no_hint(fm, station):
    m, _rel, cs = _setup(fm, station, module_key="", cs_kw={})
    record_error(cs, Mode.REJECTED, "nope")
    msgs = _msgs(m, Ev.MODULE_QUARANTINED)
    assert len(msgs) == 1
    assert mcuboot.KEY_UNKNOWN_HINT not in msgs[0]
    log = StationAuditLog.objects.get(module=m, event_type=Ev.MODULE_QUARANTINED)
    assert log.station_id == station.pk


@pytest.mark.django_db
def test_gave_up_has_no_hint(fm, station):
    m, _rel, cs = _setup(fm, station, module_key="", cs_kw={})
    for _ in range(DRAIN_TRANSIENT_LIMIT):
        record_error(cs, Mode.TRANSIENT, "t")
    msgs = _msgs(m, Ev.MODULE_FLASH_GAVE_UP)
    assert len(msgs) == 1
    assert mcuboot.KEY_UNKNOWN_HINT not in msgs[0]


def test_key_hint_applies_predicate():
    class M:
        uid_source = Module.UidSource.STM32_UID
        reported_boot_key = ""

    class R:
        boot_key_hash = RELEASE_KEY

    assert mcuboot.key_hint_applies(M, R, "rolled_back")
    assert not mcuboot.key_hint_applies(M, R, "transient")
    assert not mcuboot.key_hint_applies(M, R, "rejected")
    M.reported_boot_key = RELEASE_KEY
    assert not mcuboot.key_hint_applies(M, R, "rolled_back")
    R.boot_key_hash = ""
    assert mcuboot.key_hint_applies(M, R, "rolled_back")
    M.uid_source = Module.UidSource.SYNTHETIC
    assert not mcuboot.key_hint_applies(M, R, "rolled_back")


def test_boot_key_mismatch_predicate():
    class M:
        uid_source = Module.UidSource.STM32_UID
        reported_boot_key = OTHER_KEY

    class R:
        boot_key_hash = RELEASE_KEY

    assert mcuboot.boot_key_mismatch(M, R)
    M.reported_boot_key = RELEASE_KEY
    assert not mcuboot.boot_key_mismatch(M, R)
    M.reported_boot_key = ""
    assert not mcuboot.boot_key_mismatch(M, R)
    M.reported_boot_key = OTHER_KEY
    R.boot_key_hash = ""
    assert not mcuboot.boot_key_mismatch(M, R)
    R.boot_key_hash = RELEASE_KEY
    M.uid_source = Module.UidSource.SYNTHETIC
    assert not mcuboot.boot_key_mismatch(M, R)


@pytest.mark.django_db
@pytest.mark.parametrize("mode", [Mode.TRANSIENT, Mode.ROLLED_BACK, Mode.REJECTED])
def test_record_error_clears_phase_and_instructed_at(fm, station, mode):
    from django.utils import timezone

    now = timezone.now()
    _m, _rel, cs = _setup(
        fm,
        station,
        cs_kw={
            "phase": ModuleFirmwareConvergenceState.Phase.FLASHING,
            "phase_at": now,
            "instructed_at": now,
        },
    )
    record_error(cs, mode, "e")
    cs.refresh_from_db()
    assert cs.phase == ""
    assert cs.phase_at is None
    assert cs.instructed_at is None


# --- review round 1: a key-mismatch detour never auto-releases quarantine/failed ------


def _set_key(m, key):
    m.reported_boot_key = key
    m.save(update_fields=["reported_boot_key"])


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("start", "reason", "rollup", "label"),
    [
        (State.QUARANTINED, "", Module.Convergence.QUARANTINED, "Quarantäne"),
        (State.FAILED, Reason.TRANSIENT_LIMIT, Module.Convergence.FAILED, "Fehler"),
    ],
)
def test_mismatch_then_match_restores_sticky_state(fm, station, start, reason, rollup, label):
    m, _rel, cs = _setup(
        fm,
        station,
        module_key=RELEASE_KEY,
        cs_kw={
            "state": start,
            "reason_code": reason,
            "attempts": 3,
            "transient_failures": DRAIN_TRANSIENT_LIMIT,
            "last_error_mode": Mode.ROLLED_BACK,
            "last_error_message": "boom",
        },
    )
    _set_key(m, OTHER_KEY)
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == State.NOT_UPDATABLE
    assert cs.state_before_not_updatable == start

    _set_key(m, RELEASE_KEY)
    out = reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert out.state == start
    assert cs.state == start
    assert cs.state_before_not_updatable == ""
    assert cs.reason_code == reason
    assert cs.attempts == 3
    assert cs.transient_failures == DRAIN_TRANSIENT_LIMIT
    assert cs.last_error_mode == Mode.ROLLED_BACK
    assert cs.last_error_message == "boom"
    assert m.firmware_convergence == rollup
    msgs = _msgs(m, Ev.MODULE_UPDATABLE_AGAIN)
    assert len(msgs) == 1
    assert "Bootloader-Schlüssel passt wieder" in msgs[0]
    assert f"Zustand {label} bleibt bestehen" in msgs[0]
    assert start not in msgs[0]
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == start
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 1


@pytest.mark.django_db
def test_mismatch_then_match_still_releases_updating(fm, station):
    m, _rel, cs = _setup(
        fm, station, module_key=RELEASE_KEY, cs_kw={"state": State.UPDATING, "attempts": 2}
    )
    _set_key(m, OTHER_KEY)
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == State.NOT_UPDATABLE
    assert cs.state_before_not_updatable == ""
    _set_key(m, RELEASE_KEY)
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == State.UPDATING
    assert cs.attempts == 0
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 1


@pytest.mark.django_db
def test_quarantine_detour_not_released_by_reaching_version(fm, station):
    """quarantined -> mismatch -> desired version reported: stays quarantined."""
    m, _rel, cs = _setup(fm, station, module_key=RELEASE_KEY, cs_kw={"state": State.QUARANTINED})
    _set_key(m, OTHER_KEY)
    reconcile_module(m)
    m.last_reported_version = "26.09.15-01"
    m.save(update_fields=["last_reported_version"])
    reconcile_module(m)
    cs.refresh_from_db()
    m.refresh_from_db()
    assert cs.state == State.QUARANTINED
    assert cs.state_before_not_updatable == ""
    assert m.firmware_convergence == Module.Convergence.QUARANTINED
    # The trail must not end at not_updatable: the restore is audited too.
    msgs = _msgs(m, Ev.MODULE_UPDATABLE_AGAIN)
    assert len(msgs) == 1
    assert "Zielversion 26.09.15-01 erreicht" in msgs[0]
    assert "Zustand Quarantäne bleibt bestehen" in msgs[0]
    assert "quarantined" not in msgs[0]


@pytest.mark.django_db
def test_failed_detour_cleared_when_version_reached(fm, station):
    m, _rel, cs = _setup(
        fm,
        station,
        module_key=RELEASE_KEY,
        cs_kw={"state": State.FAILED, "reason_code": Reason.TRANSIENT_LIMIT},
    )
    _set_key(m, OTHER_KEY)
    reconcile_module(m)
    m.last_reported_version = "26.09.15-01"
    m.save(update_fields=["last_reported_version"])
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == State.OK
    assert cs.state_before_not_updatable == ""
    # The trail must not end at not_updatable: reaching the version is audited.
    msgs = _msgs(m, Ev.MODULE_UPDATABLE_AGAIN)
    assert msgs == [f"Modul {m.uid}: Zielversion 26.09.15-01 erreicht."]


@pytest.mark.django_db
def test_not_updatable_reaching_version_audits_updatable_again(fm, station):
    """not_updatable (e.g. bench SWD reflash to the target) -> OK must close the
    not_updatable/updatable-again pair (spec §6), exactly once."""
    m, _rel, cs = _setup(
        fm,
        station,
        module_key=OTHER_KEY,
        cs_kw={"state": State.NOT_UPDATABLE, "reason_code": Reason.BOOT_KEY_MISMATCH},
    )
    m.last_reported_version = "26.09.15-01"
    m.save(update_fields=["last_reported_version"])
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == State.OK
    assert _msgs(m, Ev.MODULE_UPDATABLE_AGAIN) == [
        f"Modul {m.uid}: Zielversion 26.09.15-01 erreicht."
    ]
    reconcile_module(m)
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("start", [State.UPDATING, State.FAILED])
def test_reaching_version_from_other_states_writes_no_updatable_again(fm, station, start):
    """Only a not_updatable row pairs its audit; failed has no fitting event."""
    m, _rel, cs = _setup(fm, station, module_key=RELEASE_KEY, cs_kw={"state": start})
    m.last_reported_version = "26.09.15-01"
    m.save(update_fields=["last_reported_version"])
    reconcile_module(m)
    cs.refresh_from_db()
    assert cs.state == State.OK
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 0


# --- review round 4: decisions use the DB values, not a stale in-memory module -------


@pytest.mark.django_db
def test_stale_matching_key_in_memory_does_not_release_not_updatable(fm, station):
    """An inventory recorded a mismatching key (row went not_updatable) after
    this ``module`` object was read: the stale matching key in memory must not
    release the row back to updating."""
    m, _rel, cs = _setup(
        fm,
        station,
        module_key=OTHER_KEY,
        cs_kw={"state": State.NOT_UPDATABLE, "reason_code": Reason.BOOT_KEY_MISMATCH},
    )
    m.reported_boot_key = RELEASE_KEY  # stale in-memory value, never saved
    out = reconcile_module(m)
    cs.refresh_from_db()
    assert out.state == State.NOT_UPDATABLE
    assert cs.state == State.NOT_UPDATABLE
    assert _count(m, Ev.MODULE_UPDATABLE_AGAIN) == 0


@pytest.mark.django_db
def test_stale_version_in_memory_uses_db_version(fm, station):
    m, rel, cs = _setup(fm, station, cs_kw={"state": State.UPDATING})
    Module.objects.filter(pk=m.pk).update(last_reported_version=rel.version)
    out = reconcile_module(m)  # in-memory m still reports the old version
    assert out.state == State.OK


@pytest.mark.django_db
def test_stale_uid_source_in_memory_uses_db_value(fm, station):
    m, _rel, _cs = _setup(fm, station, module_key=OTHER_KEY, synthetic=True)
    Module.objects.filter(pk=m.pk).update(uid_source=Module.UidSource.STM32_UID)
    out = reconcile_module(m)  # in-memory m is still synthetic (skips key check)
    assert out.state == State.NOT_UPDATABLE
