"""Stale StationModule cleanup in ``registry.apply_inventory``.

(1) Re-plug: the same tracked module (uid) reported in another slot removes its old
    row and carries its settings (``last_state`` + persisted calibration) over.
(2) TTL fallback: offline rows not seen for ``CONTROL_STALE_MODULE_TTL_SECONDS`` are
    pruned — only while processing a non-empty inventory snapshot.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.control import registry
from apps.control.models import PersistedCapability, StationModule
from apps.module_firmware.models import ModuleType
from apps.stations.models import Station, StationAuditLog

TTL = timedelta(hours=48)


@pytest.fixture
def fm_type(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture
def station(db):
    return Station.objects.create(name="prune-st")


def _desc(*extra):
    return [
        {"name": "frequency", "kind": "setting", "type": "float"},
        {"name": "volume", "kind": "setting", "type": "int", "min": 1, "max": 8},
        {"name": "rssi", "kind": "telemetry", "type": "int"},
        *extra,
    ]


def _fm(uid="UID-A", state=None, desc=None, module="fm"):
    return {
        "module": module,
        "identity": {"type": "fm_transceiver", "model": "SA818", "version": "1", "uid": uid},
        "capabilities": _desc() if desc is None else desc,
        "state": {} if state is None else state,
    }


def _inv(**slots):
    return [{"slot": s, "modules": mods} for s, mods in slots.items()]


def _set_age(row, age, *, last_seen=True):
    """Backdate a row (QuerySet.update bypasses auto_now on updated_at)."""
    when = timezone.now() - age
    StationModule.objects.filter(pk=row.pk).update(
        last_seen=when if last_seen else None, updated_at=when
    )


# -- (1) re-plug --------------------------------------------------------------------


@pytest.mark.django_db
def test_replug_deletes_old_slot_row(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm()]))
    registry.apply_inventory(station, _inv(slot2=[_fm()]))

    rows = list(StationModule.objects.filter(station=station))
    assert [(r.slot, r.module_id) for r in rows] == [("slot2", "fm")]
    assert rows[0].online is True
    assert rows[0].tracked_module.uid == "UID-A"


@pytest.mark.django_db
def test_replug_carries_last_state_fresh_report_wins(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm(state={"frequency": 145.5, "volume": 3})]))
    # New slot reports only frequency (different value) — volume is carried over, the
    # freshly reported frequency wins over the stale one.
    registry.apply_inventory(station, _inv(slot2=[_fm(state={"frequency": 433.5})]))

    sm = StationModule.objects.get(station=station, slot="slot2")
    assert sm.last_state == {"frequency": 433.5, "volume": 3}


@pytest.mark.django_db
def test_replug_drops_caps_absent_from_new_descriptor(fm_type, station):
    old_desc = _desc({"name": "squelch", "kind": "setting", "type": "int"})
    registry.apply_inventory(
        station, _inv(slot1=[_fm(desc=old_desc, state={"squelch": 4, "volume": 5})])
    )
    registry.apply_inventory(station, _inv(slot2=[_fm(state={})]))

    sm = StationModule.objects.get(station=station, slot="slot2")
    assert sm.last_state == {"volume": 5}  # squelch not in the new descriptor


@pytest.mark.django_db
def test_replug_moves_persisted_capabilities(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm()]))
    PersistedCapability.objects.create(
        station=station, slot="slot1", module_id="fm", capability="volume", value=6
    )
    # A leftover row of whatever module previously sat in slot2 — the moved module's
    # own calibration must replace it.
    PersistedCapability.objects.create(
        station=station, slot="slot2", module_id="fm", capability="volume", value=2
    )

    registry.apply_inventory(station, _inv(slot2=[_fm()]))

    rows = list(PersistedCapability.objects.filter(station=station))
    assert [(r.slot, r.module_id, r.capability, r.value) for r in rows] == [
        ("slot2", "fm", "volume", 6)
    ]


@pytest.mark.django_db
def test_replug_moved_persisted_value_is_reapplied(fm_type, station):
    from apps.control import persistence

    registry.apply_inventory(station, _inv(slot1=[_fm(state={"volume": 6})]))
    PersistedCapability.objects.create(
        station=station, slot="slot1", module_id="fm", capability="volume", value=6
    )
    # The module lost its calibration while being re-plugged (FW persists nothing).
    slots = _inv(slot2=[_fm(state={"volume": 1})])
    registry.apply_inventory(station, slots)

    frames = persistence.reapply_frames(station, slots)
    assert [(f["slot"], f["capability"], f["value"]) for f in frames] == [("slot2", "volume", 6)]


@pytest.mark.django_db
def test_replug_is_audited(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm()]))
    registry.apply_inventory(station, _inv(slot2=[_fm()]))

    log = StationAuditLog.objects.filter(
        station=station, message__icontains="moved from slot slot1"
    )
    assert log.count() == 1
    assert log.first().module.uid == "UID-A"


@pytest.mark.django_db
def test_same_uid_reported_in_both_slots_deletes_nothing(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm()]))
    registry.apply_inventory(station, _inv(slot1=[_fm()], slot2=[_fm()]))

    assert set(StationModule.objects.filter(station=station).values_list("slot", flat=True)) == {
        "slot1",
        "slot2",
    }


@pytest.mark.django_db
def test_different_uid_in_other_slot_keeps_old_row(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm(uid="UID-A")]))
    registry.apply_inventory(station, _inv(slot2=[_fm(uid="UID-B")]))

    old = StationModule.objects.get(station=station, slot="slot1")
    assert old.online is False  # soft-offline only — not the same physical module


@pytest.mark.django_db
def test_replug_does_not_touch_other_stations(fm_type, station):
    other = Station.objects.create(name="other-st")
    registry.apply_inventory(other, _inv(slot1=[_fm(uid="UID-A")]))
    registry.apply_inventory(station, _inv(slot2=[_fm(uid="UID-A")]))

    assert StationModule.objects.filter(station=other, slot="slot1").exists()


@pytest.mark.django_db
def test_legacy_module_without_uid_is_never_moved(station):
    legacy = _fm()
    legacy["identity"].pop("uid")
    registry.apply_inventory(station, _inv(slot1=[legacy]))
    registry.apply_inventory(station, _inv(slot2=[legacy]))

    assert StationModule.objects.filter(station=station).count() == 2


# -- (2) TTL fallback ---------------------------------------------------------------


def _online(station):
    """Station is up with a module online — a regular (non-reconnect) snapshot follows."""
    registry.apply_inventory(station, _inv(slot1=[_fm()]))


@pytest.mark.django_db
def test_ttl_prunes_old_offline_row_on_nonempty_snapshot(fm_type, station):
    _online(station)
    stale = StationModule.objects.create(station=station, slot="slot9", module_id="gps")
    _set_age(stale, TTL + timedelta(minutes=1))

    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    assert not StationModule.objects.filter(pk=stale.pk).exists()
    assert StationAuditLog.objects.filter(station=station, message__icontains="slot9/gps").exists()


@pytest.mark.django_db
def test_ttl_keeps_recent_offline_row(fm_type, station):
    _online(station)
    recent = StationModule.objects.create(station=station, slot="slot9", module_id="gps")
    _set_age(recent, TTL - timedelta(minutes=5))

    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    assert StationModule.objects.filter(pk=recent.pk).exists()


@pytest.mark.django_db
def test_ttl_null_last_seen_falls_back_to_updated_at(fm_type, station):
    _online(station)
    old = StationModule.objects.create(station=station, slot="slot8", module_id="gps")
    _set_age(old, TTL + timedelta(hours=1), last_seen=False)
    young = StationModule.objects.create(station=station, slot="slot7", module_id="gps")
    _set_age(young, timedelta(hours=1), last_seen=False)

    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    assert not StationModule.objects.filter(pk=old.pk).exists()
    assert StationModule.objects.filter(pk=young.pk).exists()


@pytest.mark.django_db
def test_ttl_no_prune_on_empty_snapshot(station):
    stale = StationModule.objects.create(station=station, slot="slot9", module_id="gps")
    _set_age(stale, TTL * 3)

    registry.apply_inventory(station, [])
    registry.apply_inventory(station, None)

    assert StationModule.objects.filter(pk=stale.pk).exists()


@pytest.mark.django_db
def test_ttl_no_prune_on_station_offline(station):
    stale = StationModule.objects.create(
        station=station, slot="slot9", module_id="gps", online=True
    )
    _set_age(stale, TTL * 3)

    registry.mark_station_offline(station)

    assert StationModule.objects.filter(pk=stale.pk).exists()


@pytest.mark.django_db
def test_ttl_never_prunes_reported_module_with_old_last_seen(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm()]))
    sm = StationModule.objects.get(station=station, slot="slot1")
    _set_age(sm, TTL * 2)

    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    assert StationModule.objects.filter(pk=sm.pk, online=True).exists()


@pytest.mark.django_db
def test_module_going_offline_now_is_not_pruned_immediately(fm_type, station):
    """``last_seen`` is only stamped on inventory frames, which the agent sends rarely
    (reconnect / topology change). A module online for days that is pulled now must
    count as seen *now* — it starts its TTL when it goes offline, not at its last
    inventory."""
    registry.apply_inventory(station, _inv(slot1=[_fm()], slot3=[_fm(uid="UID-G", module="fm")]))
    pulled = StationModule.objects.get(station=station, slot="slot3")
    _set_age(pulled, TTL * 2)

    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    pulled.refresh_from_db()
    assert pulled.online is False
    assert timezone.now() - pulled.last_seen < timedelta(minutes=1)


@pytest.mark.django_db
def test_station_offline_stamps_last_seen_of_online_modules(station):
    sm = StationModule.objects.create(station=station, slot="slot1", module_id="fm", online=True)
    _set_age(sm, TTL * 2)
    already_off = StationModule.objects.create(station=station, slot="slot2", module_id="fm")
    _set_age(already_off, TTL * 2)

    registry.mark_station_offline(station)

    sm.refresh_from_db()
    already_off.refresh_from_db()
    assert sm.online is False
    assert timezone.now() - sm.last_seen < timedelta(minutes=1)
    # An already-offline row keeps its real last-seen time (TTL keeps running).
    assert timezone.now() - already_off.last_seen > TTL


@pytest.mark.django_db
def test_ttl_is_configurable(fm_type, station, settings):
    _online(station)
    settings.CONTROL_STALE_MODULE_TTL_SECONDS = 3600
    stale = StationModule.objects.create(station=station, slot="slot9", module_id="gps")
    _set_age(stale, timedelta(hours=2))

    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    assert not StationModule.objects.filter(pk=stale.pk).exists()


@pytest.mark.django_db
def test_ttl_prune_keeps_persisted_capabilities(fm_type, station):
    _online(station)
    """Calibration is keyed by slot and survives a TTL prune (a module back from repair
    in the same slot gets it re-applied) — only the panel row goes."""
    stale = StationModule.objects.create(station=station, slot="slot9", module_id="fm")
    _set_age(stale, TTL * 2)
    PersistedCapability.objects.create(
        station=station, slot="slot9", module_id="fm", capability="volume", value=4
    )

    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    assert not StationModule.objects.filter(pk=stale.pk).exists()
    assert PersistedCapability.objects.filter(station=station, slot="slot9").exists()


@pytest.mark.django_db
def test_replug_drops_all_leftover_calibration_of_new_slot(fm_type, station):
    """Leftovers of the new slot's previous occupant go entirely — also caps the moved
    module has no value for, and also when it brings no calibration at all."""
    registry.apply_inventory(station, _inv(slot1=[_fm()]))
    PersistedCapability.objects.create(
        station=station, slot="slot2", module_id="fm", capability="volume", value=2
    )
    PersistedCapability.objects.create(
        station=station, slot="slot2", module_id="fm", capability="squelch", value=3
    )

    registry.apply_inventory(station, _inv(slot2=[_fm()]))

    assert not PersistedCapability.objects.filter(station=station).exists()


@pytest.mark.django_db
def test_ttl_does_not_count_station_outage(fm_type, station):
    """After an outage longer than the TTL, a module missing from the reconnect
    snapshot starts its TTL now — it was present when the station dropped."""
    registry.apply_inventory(station, _inv(slot1=[_fm()], slot2=[_fm(uid="UID-B")]))
    registry.mark_station_offline(station)
    StationModule.objects.filter(station=station).update(last_seen=timezone.now() - TTL * 2)
    # A row that was already gone long before the outage still expires.
    ancient = StationModule.objects.create(station=station, slot="slot9", module_id="gps")
    _set_age(ancient, TTL * 5)

    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    missed = StationModule.objects.get(station=station, slot="slot2")
    assert missed.online is False
    assert timezone.now() - missed.last_seen < timedelta(minutes=1)
    assert not StationModule.objects.filter(pk=ancient.pk).exists()


def _calibration(station):
    return sorted(
        PersistedCapability.objects.filter(station=station).values_list(
            "slot", "capability", "value"
        )
    )


def _calibrate(station, slot, value, capability="volume"):
    PersistedCapability.objects.create(
        station=station, slot=slot, module_id="fm", capability=capability, value=value
    )


@pytest.mark.django_db
def test_rotation_moves_each_modules_calibration(fm_type, station):
    """A slot1->slot2 while B slot2->slot3: the (slot2, fm) row is reused for A, yet B
    keeps its own calibration and A brings its own."""
    registry.apply_inventory(station, _inv(slot1=[_fm("UID-A")], slot2=[_fm("UID-B")]))
    _calibrate(station, "slot1", 6)
    _calibrate(station, "slot2", 3)

    registry.apply_inventory(station, _inv(slot2=[_fm("UID-A")], slot3=[_fm("UID-B")]))

    assert _calibration(station) == [("slot2", "volume", 6), ("slot3", "volume", 3)]
    assert sorted(
        StationModule.objects.filter(station=station).values_list("slot", flat=True)
    ) == [
        "slot2",
        "slot3",
    ]


@pytest.mark.django_db
def test_rotation_carries_last_state_of_reused_row(fm_type, station):
    registry.apply_inventory(
        station,
        _inv(slot1=[_fm("UID-A", state={"volume": 6})], slot2=[_fm("UID-B", state={"volume": 3})]),
    )
    registry.apply_inventory(station, _inv(slot2=[_fm("UID-A")], slot3=[_fm("UID-B")]))

    assert StationModule.objects.get(station=station, slot="slot2").last_state == {"volume": 6}
    assert StationModule.objects.get(station=station, slot="slot3").last_state == {"volume": 3}


@pytest.mark.django_db
def test_swap_exchanges_calibration(fm_type, station):
    registry.apply_inventory(station, _inv(slot1=[_fm("UID-A")], slot2=[_fm("UID-B")]))
    _calibrate(station, "slot1", 6)
    _calibrate(station, "slot2", 3)

    registry.apply_inventory(station, _inv(slot1=[_fm("UID-B")], slot2=[_fm("UID-A")]))

    assert _calibration(station) == [("slot1", "volume", 3), ("slot2", "volume", 6)]


@pytest.mark.django_db
def test_new_module_in_vacated_slot_gets_no_calibration(fm_type, station):
    """Calibration left with its module; a new module in the old slot starts clean."""
    registry.apply_inventory(station, _inv(slot1=[_fm("UID-A")]))
    _calibrate(station, "slot1", 6)

    registry.apply_inventory(station, _inv(slot1=[_fm("UID-N")], slot2=[_fm("UID-A")]))

    assert _calibration(station) == [("slot2", "volume", 6)]


@pytest.mark.django_db
def test_ttl_expires_without_outage_when_no_module_is_online(fm_type, station):
    """The only module was removed while the station stayed up: repeated non-empty
    snapshots must not keep restarting its TTL."""
    registry.apply_inventory(station, _inv(slot1=[_fm()]))
    registry.apply_inventory(station, [{"slot": "slot1", "modules": []}])
    sm = StationModule.objects.get(station=station, slot="slot1")
    _set_age(sm, TTL * 3)

    registry.apply_inventory(station, [{"slot": "slot1", "modules": []}])

    assert not StationModule.objects.filter(pk=sm.pk).exists()


@pytest.mark.django_db
def test_outage_flag_survives_empty_snapshot(fm_type, station):
    """``slots == []`` (transient discovery failure) neither prunes nor consumes the
    outage flag; the next real snapshot restarts the TTL."""
    registry.apply_inventory(station, _inv(slot1=[_fm()], slot2=[_fm("UID-B")]))
    registry.mark_station_offline(station)
    StationModule.objects.filter(station=station).update(last_seen=timezone.now() - TTL * 2)

    registry.apply_inventory(station, [])
    registry.apply_inventory(station, _inv(slot1=[_fm()]))

    missed = StationModule.objects.get(station=station, slot="slot2")
    assert timezone.now() - missed.last_seen < timedelta(minutes=1)
    assert missed.offline_with_station is False
