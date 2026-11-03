"""Task 6: central, read-only module_update_status / bulk_update_status (spec §4)."""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.control.models import ControlLock
from apps.module_firmware.mcuboot import (
    DEV_KEY_HASH_PREFIX,
    KEY_UNKNOWN_HINT,
    NOT_UPDATABLE_DEV_TEXT,
)
from apps.module_firmware.models import (
    DRAIN_TRANSIENT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleType,
)
from apps.module_firmware.update_status import (
    REASON_CODES,
    UpdateState,
    WaitReason,
    bulk_update_status,
    module_update_status,
)
from apps.stations.models import StationAuditLog
from tests.module_firmware.update_status_helpers import (
    TARGET,
    _cs,
    _module,
    _online,
    _release,
    _target,
)

State = ModuleFirmwareConvergenceState.State
RELEASE_KEY = "1111111111111111"


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture
def base(fm, station):
    """Online dfu station, fleet target, matching release, drifted module."""
    _online(station)
    _target(fm)
    rel = _release(fm)
    m = _module(fm, station)
    return station, rel, m


def test_no_target(fm, station):
    _online(station)
    m = _module(fm, station)
    s = module_update_status(m)
    assert s.state == "no_target" and s.reason_text == "Kein Ziel"
    assert s.reason_code == "" and s.since is None and s.phase == ""


def test_no_open_assignment_is_no_target(fm):
    _target(fm)
    m = _module(fm, None)
    s = module_update_status(m)
    assert s.state == UpdateState.NO_TARGET and s.reason_text == "Kein Ziel"


def test_current(base):
    station, rel, m = base
    m.last_reported_version = TARGET
    m.save()
    _cs(m, rel, state=State.QUARANTINED, attempts=3, last_error_mode="rolled_back")
    s = module_update_status(m)
    assert s.state == "current" and s.reason_text == "Aktuell" and s.since is None


def test_current_wins_over_key_mismatch(fm, station):
    """Mirror of the reconciler guard: on the desired version a key mismatch is
    never shown as not_updatable."""
    _online(station)
    _target(fm)
    rel = _release(fm, boot_key=RELEASE_KEY)
    m = _module(fm, station, version=TARGET, key="2222222222222222")
    _cs(m, rel, state=State.QUARANTINED, attempts=3, last_error_mode="rolled_back")
    s = module_update_status(m)
    assert s.state == "current"


def test_quarantined_rolled_back_text(base):
    station, rel, m = base
    rel.boot_key_hash = RELEASE_KEY
    rel.save()
    m.reported_boot_key = RELEASE_KEY
    m.save()
    cs = _cs(m, rel, state=State.QUARANTINED, attempts=3, last_error_mode="rolled_back")
    s = module_update_status(m)
    assert s.state == "quarantined"
    assert s.reason_code == "rolled_back"
    assert s.reason_text == "Quarantäne (Update 3× zurückgerollt)"
    assert s.since == cs.updated_at


def test_quarantined_rejected_text(base):
    station, rel, m = base
    _cs(m, rel, state=State.QUARANTINED, attempts=0, last_error_mode="rejected")
    s = module_update_status(m)
    assert s.reason_code == "rejected"
    assert s.reason_text == "Quarantäne (vom Modul abgelehnt)"


def test_quarantined_with_key_hint(base):
    station, rel, m = base  # neither key known -> hint applies
    _cs(m, rel, state=State.QUARANTINED, attempts=3, last_error_mode="rolled_back")
    s = module_update_status(m)
    assert KEY_UNKNOWN_HINT in s.reason_text
    assert s.reason_text == f"Quarantäne (Update 3× zurückgerollt – {KEY_UNKNOWN_HINT})"


def test_not_updatable_dev_text(base):
    station, rel, m = base
    m.reported_boot_key = DEV_KEY_HASH_PREFIX
    m.save()
    cs = _cs(m, rel, state=State.NOT_UPDATABLE, reason_code="boot_key_mismatch")
    s = module_update_status(m)
    assert s.state == "not_updatable" and s.reason_code == "boot_key_mismatch"
    assert s.reason_text == NOT_UPDATABLE_DEV_TEXT
    assert s.since == cs.updated_at


def test_failed_shows_last_message(base):
    station, rel, m = base
    cs = _cs(
        m,
        rel,
        state=State.FAILED,
        reason_code="transient_limit",
        last_error_message="dfu-util timeout",
    )
    s = module_update_status(m)
    assert s.state == "failed" and s.reason_code == "transient_limit"
    assert s.reason_text == "Fehler: dfu-util timeout"
    assert s.since == cs.updated_at


def test_failed_without_message(base):
    station, rel, m = base
    _cs(m, rel, state=State.FAILED, reason_code="transient_limit")
    assert module_update_status(m).reason_text == "Fehler: Update mehrfach fehlgeschlagen"


def test_offline_beats_running_phase(base):
    station, rel, m = base
    station.last_seen = timezone.now() - timedelta(minutes=10)
    station.save(update_fields=["last_seen"])
    _cs(m, rel, phase="flashing", phase_at=timezone.now())
    s = module_update_status(m)
    assert s.state == "waiting" and s.reason_code == "station_offline"
    assert s.reason_text == "Wartet: Station offline" and s.since is None


def test_running_phase_text(base):
    station, rel, m = base
    cs = _cs(m, rel, phase="flashing", phase_at=timezone.now())
    s = module_update_status(m)
    assert s.state == "running" and s.phase == "flashing"
    assert s.reason_text == "Update läuft: Flashen" and s.since == cs.phase_at


@pytest.mark.parametrize(
    "phase,text",
    [("downloading", "Update läuft: Download"), ("verifying", "Update läuft: Prüfen")],
)
def test_running_other_phases(base, phase, text):
    station, rel, m = base
    _cs(m, rel, phase=phase, phase_at=timezone.now())
    assert module_update_status(m).reason_text == text


def test_old_agent_waits(base):
    station, rel, m = base
    station.agent_capabilities = {}
    station.save(update_fields=["agent_capabilities"])
    s = module_update_status(m)
    assert s.state == "waiting" and s.reason_code == "agent_no_module_ota"
    assert s.reason_text == "Wartet: Station-Agent kann keine Module flashen (Image zu alt)"


def test_no_release_for_variant_text(fm, station):
    _online(station)
    _target(fm)
    _release(fm, variant="uhf")
    m = _module(fm, station, variant="vhf")
    s = module_update_status(m)
    assert s.state == "waiting" and s.reason_code == "no_release_for_variant"
    assert s.reason_text == "Wartet: Keine Firmware 26.09.15-01 für Variante vhf vorhanden"


def test_no_release_for_empty_variant_uses_dash(fm, station):
    _online(station)
    _target(fm)
    m = _module(fm, station, variant="")
    s = module_update_status(m)
    assert s.reason_text == "Wartet: Keine Firmware 26.09.15-01 für Variante – vorhanden"


def test_archived_release_counts_as_missing(base):
    """Mirrors reconcile_module: the default manager hides archived releases."""
    station, rel, m = base
    _cs(m, rel, state=State.QUARANTINED, attempts=3, last_error_mode="rolled_back")
    rel.archive()
    s = module_update_status(m)
    assert s.reason_code == "no_release_for_variant"


def test_no_artifact_sim(fm, station):
    _online(station, backends=("sim",))
    _target(fm)
    _release(fm, sim=False)
    m = _module(fm, station, synthetic=True)
    s = module_update_status(m)
    assert s.state == "waiting" and s.reason_code == "no_artifact"
    assert s.reason_text == "Wartet: Release enthält kein passendes Artefakt (sim)"


def test_busy_station(base, admin_user):
    station, rel, m = base
    ControlLock.objects.create(
        station=station, scope="station", holder=admin_user, acquired_at=timezone.now()
    )
    s = module_update_status(m)
    assert s.state == "waiting" and s.reason_code == "station_busy"
    assert s.reason_text == "Wartet: Station belegt (Bedienung aktiv)"


def test_released_lock_is_not_busy(base):
    station, rel, m = base
    ControlLock.objects.create(station=station, scope="station", holder=None)
    assert module_update_status(m).state == "pending"


def test_pending(base):
    station, rel, m = base
    cs = _cs(m, rel)
    s = module_update_status(m)
    assert s.state == "pending" and s.reason_text == "Update ausstehend"
    assert s.reason_code == "" and s.since == cs.created_at


def test_pending_without_row_has_no_since(base):
    station, rel, m = base
    s = module_update_status(m)
    assert s.state == "pending" and s.since is None


def test_quarantine_beats_offline(base):
    station, rel, m = base
    station.last_seen = timezone.now() - timedelta(minutes=10)
    station.save(update_fields=["last_seen"])
    _cs(m, rel, state=State.QUARANTINED, attempts=0, last_error_mode="rejected")
    assert module_update_status(m).state == "quarantined"


def test_station_kwarg_overrides_assignment(fm, station, station_factory):
    _target(fm)
    _release(fm)
    other = _online(station_factory())
    station.last_seen = None
    station.save(update_fields=["last_seen"])
    m = _module(fm, station)
    assert module_update_status(m).reason_code == "station_offline"
    assert module_update_status(m, station=other).state == "pending"


def test_is_live_flags(base):
    station, rel, m = base
    assert module_update_status(m).is_live  # pending
    cs = _cs(m, rel, phase="verifying", phase_at=timezone.now())
    assert module_update_status(m).is_live  # running
    station.agent_capabilities = {}
    station.save(update_fields=["agent_capabilities"])
    cs.phase = ""
    cs.save()
    assert module_update_status(m).is_live  # waiting
    cs.state = State.FAILED
    cs.save()
    s = module_update_status(m)
    assert s.state == "failed" and not s.is_live
    m.last_reported_version = TARGET
    m.save()
    assert not module_update_status(m).is_live


def test_short_labels():
    from apps.module_firmware.update_status import UpdateStatus

    labels = {
        "no_target": "Kein Ziel",
        "current": "Aktuell",
        "quarantined": "Quarantäne",
        "not_updatable": "Nicht updatebar",
        "failed": "Fehler",
        "waiting": "Wartet",
        "running": "Update läuft",
        "pending": "Update ausstehend",
    }
    assert set(labels) == set(UpdateState.values)
    for state, label in labels.items():
        assert UpdateStatus(state, "", "", "", None).short_label == label


def test_reason_codes_enum():
    assert set(REASON_CODES) == (
        set(WaitReason.values)
        | set(ModuleFirmwareConvergenceState.ReasonCode.values)
        | {"rejected", "rolled_back"}
    )


def _mixed_fleet(fm, station_factory, admin_user):
    """8 modules on 3 stations covering most states."""
    s1 = _online(station_factory())
    s2 = _online(station_factory(), backends=())
    s3 = _online(station_factory(), backends=("dfu", "sim"))
    ControlLock.objects.create(
        station=s3, scope="station", holder=admin_user, acquired_at=timezone.now()
    )
    _target(fm)
    rel = _release(fm)
    ms = [
        _module(fm, s1, slot="a"),  # pending
        _module(fm, s1, slot="b", version=TARGET),  # current
        _module(fm, s1, slot="c"),  # quarantined
        _module(fm, s1, slot="d", variant="uhf"),  # no release for variant
        _module(fm, s2, slot="a"),  # old agent
        _module(fm, s3, slot="a"),  # busy
        _module(fm, s3, slot="b", synthetic=True),  # no sim artifact
        _module(fm, s3, slot="c"),  # running
    ]
    _cs(ms[2], rel, state=State.QUARANTINED, attempts=3, last_error_mode="rolled_back")
    _cs(ms[7], rel, phase="downloading", phase_at=timezone.now())
    return ms


def _fresh(ms):
    return list(Module.objects.filter(pk__in=[m.pk for m in ms]).order_by("pk"))


def test_bulk_query_count_constant(fm, station_factory, admin_user):
    ms = _mixed_fleet(fm, station_factory, admin_user)
    one = _fresh(ms[:1])
    many = _fresh(ms)
    with CaptureQueriesContext(connection) as q1:
        bulk_update_status(one)
    with CaptureQueriesContext(connection) as q8:
        result = bulk_update_status(many)
    assert len(q1) == len(q8) == 5
    assert {s.state for s in result.values()} >= {
        "pending",
        "current",
        "quarantined",
        "waiting",
        "running",
    }


def test_bulk_respects_open_assignments_prefetch(fm, station_factory, admin_user):
    from django.db.models import Prefetch

    ms = _mixed_fleet(fm, station_factory, admin_user)
    qs = Module.objects.filter(pk__in=[m.pk for m in ms]).prefetch_related(
        Prefetch(
            "assignments",
            queryset=ModuleAssignmentHistory.objects.filter(to_ts__isnull=True).select_related(
                "station"
            ),
            to_attr="open_assignments",
        )
    )
    modules = list(qs)
    with CaptureQueriesContext(connection) as q:
        prefetched = bulk_update_status(modules)
    assert len(q) == 4
    assert prefetched == bulk_update_status(_fresh(ms))


def test_bulk_matches_single(fm, station_factory, admin_user):
    ms = _mixed_fleet(fm, station_factory, admin_user)
    bulk = bulk_update_status(_fresh(ms))
    assert set(bulk) == {m.pk for m in ms}
    for m in _fresh(ms):
        assert bulk[m.pk] == module_update_status(m)


def test_bulk_empty():
    assert bulk_update_status([]) == {}


def test_status_is_read_only(base):
    station, rel, m = base
    cs = _cs(m, rel, state=State.QUARANTINED, attempts=3, last_error_mode="rolled_back")
    before_logs = StationAuditLog.objects.count()
    before_updated = cs.updated_at
    before_rollup = Module.objects.get(pk=m.pk).firmware_convergence
    module_update_status(m)
    bulk_update_status([m])
    cs.refresh_from_db()
    assert StationAuditLog.objects.count() == before_logs
    assert cs.updated_at == before_updated
    assert Module.objects.get(pk=m.pk).firmware_convergence == before_rollup
    assert ModuleFirmwareConvergenceState.objects.count() == 1


def test_updating_row_at_transient_cap_shows_failed(base):
    station, rel, m = base
    cs = _cs(
        m, rel, transient_failures=DRAIN_TRANSIENT_LIMIT, last_error_message="dfu-util timeout"
    )
    s = module_update_status(m)
    assert s.state == "failed" and s.reason_code == "transient_limit"
    assert s.reason_text == "Fehler: dfu-util timeout" and s.since == cs.updated_at


def test_at_cap_row_failed_even_when_offline(base):
    station, rel, m = base
    station.last_seen = timezone.now() - timedelta(days=3)
    station.save(update_fields=["last_seen"])
    _cs(m, rel, transient_failures=DRAIN_TRANSIENT_LIMIT)
    s = module_update_status(m)
    assert s.state == "failed"
    assert s.reason_text == "Fehler: Update mehrfach fehlgeschlagen"


def test_below_cap_is_not_failed(base):
    station, rel, m = base
    _cs(m, rel, transient_failures=DRAIN_TRANSIENT_LIMIT - 1)
    assert module_update_status(m).state == "pending"


def test_key_mismatch_without_row_is_not_updatable(base):
    station, rel, m = base
    rel.boot_key_hash = RELEASE_KEY
    rel.save()
    m.reported_boot_key = DEV_KEY_HASH_PREFIX
    m.save()
    s = module_update_status(m)
    assert s.state == "not_updatable" and s.reason_code == "boot_key_mismatch"
    assert s.reason_text == NOT_UPDATABLE_DEV_TEXT and s.since is None


@pytest.mark.parametrize("row_state", [State.UPDATING, State.FAILED, State.QUARANTINED])
def test_key_mismatch_overrides_row_states(base, row_state):
    station, rel, m = base
    rel.boot_key_hash = RELEASE_KEY
    rel.save()
    m.reported_boot_key = DEV_KEY_HASH_PREFIX
    m.save()
    cs = _cs(m, rel, state=row_state, attempts=3, last_error_mode="rolled_back")
    s = module_update_status(m)
    assert s.state == "not_updatable" and s.since == cs.updated_at


def test_key_mismatch_loses_to_current(base):
    station, rel, m = base
    rel.boot_key_hash = RELEASE_KEY
    rel.save()
    m.reported_boot_key = DEV_KEY_HASH_PREFIX
    m.last_reported_version = TARGET
    m.save()
    assert module_update_status(m).state == "current"


def test_key_fixed_releases_not_updatable(base):
    """Mirrors reconciler step 3: matching keys again -> retried (pending)."""
    station, rel, m = base
    rel.boot_key_hash = RELEASE_KEY
    rel.save()
    m.reported_boot_key = RELEASE_KEY
    m.save()
    _cs(m, rel, state=State.NOT_UPDATABLE, reason_code="boot_key_mismatch", transient_failures=9)
    assert module_update_status(m).state == "pending"


def test_status_fields_are_plain_str(base):
    station, rel, m = base
    cs = _cs(m, rel, phase="flashing", phase_at=timezone.now())
    for s in (module_update_status(m), module_update_status(_module(m.module_type, None))):
        assert type(s.state) is str and type(s.reason_code) is str and type(s.phase) is str
    cs.phase = ""
    cs.save()
    station.agent_capabilities = {}
    station.save(update_fields=["agent_capabilities"])
    s = module_update_status(m)
    assert type(s.reason_code) is str and s.reason_code == "agent_no_module_ota"


def test_bulk_with_station_override(fm, station_factory, admin_user):
    ms = _mixed_fleet(fm, station_factory, admin_user)
    s1 = ModuleAssignmentHistory.objects.get(module=ms[0]).station
    on_s1 = [m for m in _fresh(ms) if ModuleAssignmentHistory.objects.get(module=m).station == s1]
    with CaptureQueriesContext(connection) as q_plain:
        plain = bulk_update_status(on_s1)
    with CaptureQueriesContext(connection) as q_override:
        override = bulk_update_status(on_s1, station=s1)
    assert override == plain
    assert len(q_override) <= len(q_plain)
    for m in on_s1:
        assert override[m.pk] == module_update_status(m, station=s1)
