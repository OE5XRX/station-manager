"""Persistence + drift re-apply of role-gated calibration capabilities (spec §4a)."""

import asyncio

import pytest
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.control import persistence
from apps.control.models import PersistedCapability, StationModule
from apps.stations.models import Station, StationAuditLog
from config.asgi import application

DESC = [
    {"name": "filter_hpf", "kind": "setting", "type": "bool"},
    {"name": "filter_lpf", "kind": "setting", "type": "bool"},
    {"name": "rssi", "kind": "telemetry", "type": "int"},
]


def _inv(state, module="fm0", slot=1, desc=DESC, mtype="fm"):
    return [
        {
            "slot": slot,
            "modules": [
                {
                    "module": module,
                    "identity": {"type": mtype},
                    "capabilities": desc,
                    "state": state,
                }
            ],
        }
    ]


@pytest.fixture
def staff_user(db):
    return User.objects.create(username="pc-staff", membership_level=User.MembershipLevel.STAFF)


@pytest.fixture
def other_station(db):
    return Station.objects.create(name="Other Station", callsign="OE5OTH")


# -- save / reapply_frames (pure) ---------------------------------------------


@pytest.mark.django_db
def test_save_upserts(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    persistence.save(station, 1, "fm0", "filter_hpf", True, staff_user)
    row = PersistedCapability.objects.get()
    assert row.value is True
    assert row.slot == "1"
    assert row.updated_by == staff_user


@pytest.mark.django_db
def test_reapply_only_on_drift(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    assert persistence.reapply_frames(station, _inv({"filter_hpf": False})) == []
    frames = persistence.reapply_frames(station, _inv({"filter_hpf": True}))
    assert len(frames) == 1
    f = frames[0]
    assert (f["v"], f["type"], f["op"], f["slot"], f["module"], f["capability"], f["value"]) == (
        1,
        "command",
        "set",
        1,
        "fm0",
        "filter_hpf",
        False,
    )
    assert f["request_id"].startswith("persist-")


@pytest.mark.django_db
def test_reapply_bool_drift_is_type_strict(station, staff_user):
    # 0 == False in Python; a numeric 0 reported for a bool cap is still drift.
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    assert len(persistence.reapply_frames(station, _inv({"filter_hpf": 0}))) == 1


@pytest.mark.django_db
def test_reapply_when_state_missing_key(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_lpf", False, staff_user)
    assert len(persistence.reapply_frames(station, _inv({}))) == 1


@pytest.mark.django_db
def test_reapply_string_slot_matches(station, staff_user):
    persistence.save(station, "slot0", "fm0", "filter_lpf", True, staff_user)
    frames = persistence.reapply_frames(station, _inv({}, slot="slot0"))
    assert [f["slot"] for f in frames] == ["slot0"]


@pytest.mark.django_db
def test_no_reapply_for_cap_not_in_descriptor_or_other_module(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    assert persistence.reapply_frames(station, _inv({}, desc=[])) == []
    assert persistence.reapply_frames(station, _inv({}, module="fm1")) == []
    assert persistence.reapply_frames(station, _inv({}, slot=2)) == []


@pytest.mark.django_db
def test_no_reapply_for_readonly_or_non_setting_cap(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    ro = [{"name": "filter_hpf", "kind": "setting", "type": "bool", "readonly": True}]
    tele = [{"name": "filter_hpf", "kind": "telemetry", "type": "bool"}]
    assert persistence.reapply_frames(station, _inv({}, desc=ro)) == []
    assert persistence.reapply_frames(station, _inv({}, desc=tele)) == []


@pytest.mark.django_db
def test_no_reapply_when_value_no_longer_fits_descriptor(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    as_int = [{"name": "filter_hpf", "kind": "setting", "type": "int"}]
    assert persistence.reapply_frames(station, _inv({"filter_hpf": 3}, desc=as_int)) == []


@pytest.mark.django_db
def test_never_reapply_non_persist_policy_caps(station, staff_user):
    # A stray row for a non-persist capability (e.g. ptt / frequency) is never re-applied:
    # re-apply frames are digital calibration settings only, never keying.
    desc = DESC + [
        {"name": "ptt", "kind": "setting", "type": "bool"},
        {"name": "frequency", "kind": "setting", "type": "int"},
    ]
    PersistedCapability.objects.create(
        station=station, slot="1", module_id="fm0", capability="ptt", value=True
    )
    PersistedCapability.objects.create(
        station=station, slot="1", module_id="fm0", capability="frequency", value=145500
    )
    assert persistence.reapply_frames(station, _inv({}, desc=desc)) == []


@pytest.mark.django_db
def test_other_station_rows_never_leak(station, other_station, staff_user):
    persistence.save(other_station, 1, "fm0", "filter_hpf", False, staff_user)
    assert persistence.reapply_frames(station, _inv({"filter_hpf": True})) == []


@pytest.mark.django_db
def test_garbage_inventory_is_ignored(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    for junk in (
        None,
        "x",
        [None],
        [{"slot": 1, "modules": "x"}],
        [{"modules": [{}]}],
        [{"slot": 1, "modules": [None, {"module": ["x"]}]}],
        [{"slot": [1], "modules": [{"module": "fm0", "capabilities": DESC}]}],
        [{"slot": 1, "modules": [{"module": "fm0", "capabilities": "x", "state": "y"}]}],
        [{"slot": 1, "modules": [{"module": "fm0", "capabilities": [None, 3], "state": {}}]}],
    ):
        assert persistence.reapply_frames(station, junk) == []


# -- persist_if_valid (type-checks against the registered descriptor) ----------


@pytest.mark.django_db
def test_persist_if_valid_type_checks_against_descriptor(station, staff_user):
    StationModule.objects.create(
        station=station, slot="1", module_id="fm0", type="fm", capability_descriptor=DESC
    )
    for bad in ("false", 0, None, {"x": 1}, [False]):
        assert persistence.persist_if_valid(station, 1, "fm0", "filter_hpf", bad, staff_user) is (
            False
        )
    assert not PersistedCapability.objects.exists()
    # Telemetry / unknown caps / unknown modules are never persisted.
    assert persistence.persist_if_valid(station, 1, "fm0", "rssi", 3, staff_user) is False
    assert persistence.persist_if_valid(station, 1, "fm0", "nope", True, staff_user) is False
    assert persistence.persist_if_valid(station, 1, "fm9", "filter_hpf", True, staff_user) is False
    assert not PersistedCapability.objects.exists()
    assert persistence.persist_if_valid(station, 1, "fm0", "filter_hpf", False, staff_user) is True
    assert PersistedCapability.objects.get().value is False


@pytest.mark.parametrize(
    "cap,value,ok",
    [
        ({"type": "bool"}, True, True),
        ({"type": "bool"}, 1, False),
        ({"type": "int"}, 3, True),
        ({"type": "int"}, True, False),
        ({"type": "int"}, 3.5, False),
        ({"type": "float"}, 3, True),
        ({"type": "float"}, 3.5, True),
        ({"type": "float"}, False, False),
        ({"type": "string"}, "abc", True),
        ({"type": "string"}, 3, False),
        ({"type": "enum", "values": ["a", "b"]}, "a", True),
        ({"type": "enum", "values": ["a", "b"]}, "c", False),
        ({"type": "enum", "values": "ab"}, "a", False),
        ({"type": "mystery"}, 1, False),
        ({"type": "bool"}, {"x": 1}, False),
    ],
)
def test_value_fits(cap, value, ok):
    assert persistence.value_fits(cap, value) is ok


# -- Channels: drift re-apply on agent inventory + persist on ok result --------

V = 1


def _agent_comm(station_id):
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _until(comm, pred, tries=12):
    for _ in range(tries):
        msg = await comm.receive_json_from(timeout=2)
        if pred(msg):
            return msg
    raise AssertionError("expected frame never arrived")


@pytest.mark.django_db(transaction=True)
def test_agent_inventory_with_drift_gets_reapply_command(control_agent_auth):
    station = Station.objects.create(name="pc-drift", status="online")
    persistence.save(station, "slot0", "fm0", "filter_hpf", False, None)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(
            {"v": V, "type": "inventory", "slots": _inv({"filter_hpf": True}, slot="slot0")}
        )
        frame = await _until(agent, lambda m: m.get("type") == "command")
        # No drift → no frame.
        await agent.send_json_to(
            {"v": V, "type": "inventory", "slots": _inv({"filter_hpf": False}, slot="slot0")}
        )
        assert await agent.receive_nothing(timeout=0.3)
        await agent.disconnect()
        return frame

    frame = asyncio.run(scenario())
    assert frame["request_id"].startswith("persist-")
    assert (frame["op"], frame["slot"], frame["module"], frame["capability"], frame["value"]) == (
        "set",
        "slot0",
        "fm0",
        "filter_hpf",
        False,
    )
    assert StationAuditLog.objects.filter(
        station=station, event_type="control_command", message__contains="re-apply persisted"
    ).exists()


def _run_staff_set(station, staff, ok):
    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(staff, station.id)
        assert (await browser.connect())[0] is True
        await browser.send_json_to({"type": "lock_acquire"})
        await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")
        await browser.send_json_to(
            {
                "type": "command",
                "request_id": "s1",
                "slot": "slot0",
                "module": "fm0",
                "capability": "filter_hpf",
                "op": "set",
                "value": False,
            }
        )
        cmd = await _until(agent, lambda m: m.get("type") == "command")
        assert cmd["request_id"] == "s1"
        result = {"v": V, "type": "result", "request_id": "s1", "ok": ok}
        if ok:
            result["value"] = False
        else:
            result["error"] = {"code": "timeout", "msg": ""}
        await agent.send_json_to(result)
        await _until(browser, lambda m: m.get("type") == "result")
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ok", [True, False])
def test_staff_set_persists_only_on_ok_result(control_agent_auth, ok):
    station = Station.objects.create(name=f"pc-set-{ok}", status="online")
    staff = User.objects.create(
        username=f"pc-st-{ok}", membership_level=User.MembershipLevel.STAFF
    )
    StationModule.objects.create(
        station=station, slot="slot0", module_id="fm0", type="fm", capability_descriptor=DESC
    )
    _run_staff_set(station, staff, ok)
    rows = list(PersistedCapability.objects.filter(station=station))
    if ok:
        assert len(rows) == 1
        assert (rows[0].slot, rows[0].module_id, rows[0].capability, rows[0].value) == (
            "slot0",
            "fm0",
            "filter_hpf",
            False,
        )
        assert rows[0].updated_by_id == staff.id
        assert StationAuditLog.objects.filter(
            station=station, message__contains="persisted filter_hpf=False"
        ).exists()
    else:
        assert rows == []


def test_admin_is_registered_read_only():
    from django.contrib import admin as dj_admin

    ma = dj_admin.site._registry[PersistedCapability]
    assert ma.has_add_permission(None) is False
    assert ma.has_change_permission(None) is False


# -- Fix round 1: persist binding / state confusion ----------------------------


def _set_frame(rid, capability="filter_hpf", value=False, slot="slot0"):
    return {
        "type": "command",
        "request_id": rid,
        "slot": slot,
        "module": "fm0",
        "capability": capability,
        "op": "set",
        "value": value,
    }


def _ok(rid, ok=True):
    frame = {"v": V, "type": "result", "request_id": rid, "ok": ok}
    if ok:
        frame["value"] = False
    else:
        frame["error"] = {"code": "timeout", "msg": ""}
    return frame


async def _acquire(browser):
    await browser.send_json_to({"type": "lock_acquire"})
    await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")


def _fm_station(name, slot="slot0"):
    station = Station.objects.create(name=name, status="online")
    StationModule.objects.create(
        station=station, slot=slot, module_id="fm0", type="fm", capability_descriptor=DESC
    )
    return station


def _staff(name):
    return User.objects.create(username=name, membership_level=User.MembershipLevel.STAFF)


@pytest.mark.django_db(transaction=True)
def test_result_for_unknown_or_mismatched_request_id_does_not_persist(control_agent_auth):
    station = _fm_station("fr-mismatch")
    staff = _staff("fr-mm")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(staff, station.id)
        assert (await browser.connect())[0] is True
        await _acquire(browser)
        await browser.send_json_to(_set_frame("r1"))
        await _until(agent, lambda m: m.get("type") == "command")
        # ok for an id that was never sent, then a failed result for the real one.
        await agent.send_json_to(_ok("r2"))
        await _until(browser, lambda m: m.get("type") == "result" and m["request_id"] == "r2")
        await agent.send_json_to(_ok("r1", ok=False))
        await _until(browser, lambda m: m.get("type") == "result" and m["request_id"] == "r1")
        # A late duplicate ok for r1 after the entry was consumed must not persist either.
        await agent.send_json_to(_ok("r1"))
        await _until(browser, lambda m: m.get("type") == "result" and m["request_id"] == "r1")
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    assert not PersistedCapability.objects.filter(station=station).exists()


@pytest.mark.django_db(transaction=True)
def test_result_after_lock_loss_does_not_persist(control_agent_auth):
    station = _fm_station("fr-lockloss")
    a = _staff("fr-a")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(a, station.id)
        assert (await browser.connect())[0] is True
        await _acquire(browser)
        await browser.send_json_to(_set_frame("r5"))
        await _until(agent, lambda m: m.get("type") == "command")
        await browser.send_json_to({"type": "lock_release"})
        await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "free")
        await agent.send_json_to(_ok("r5"))
        await _until(browser, lambda m: m.get("type") == "result")
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    assert not PersistedCapability.objects.filter(station=station).exists()


@pytest.mark.django_db(transaction=True)
def test_reused_request_id_by_new_holder_never_persists_old_holders_value(control_agent_auth):
    # Staff A has r5 (filter_hpf=False) in flight, loses the lock; new holder B reuses r5
    # for an operator-level command. B's ok must not persist A's value — on A's consumer
    # (result delivered to another consumer) nor on B's.
    station = _fm_station("fr-reuse")
    a, b = _staff("fr-ra"), _staff("fr-rb")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        ba = _browser(a, station.id)
        assert (await ba.connect())[0] is True
        bb = _browser(b, station.id)
        assert (await bb.connect())[0] is True
        await _acquire(ba)
        await ba.send_json_to(_set_frame("r5"))
        await _until(agent, lambda m: m.get("type") == "command")
        await ba.send_json_to({"type": "lock_release"})
        await _until(ba, lambda m: m.get("type") == "lock" and m.get("state") == "free")
        await _acquire(bb)
        await bb.send_json_to(_set_frame("r5", capability="frequency", value=145500))
        await _until(agent, lambda m: m.get("type") == "command")
        await agent.send_json_to(_ok("r5"))
        await _until(ba, lambda m: m.get("type") == "result")
        await _until(bb, lambda m: m.get("type") == "result")
        await ba.disconnect()
        await bb.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    assert not PersistedCapability.objects.filter(station=station).exists()


@pytest.mark.django_db(transaction=True)
def test_result_on_non_sender_consumer_does_not_persist(control_agent_auth):
    # A staff viewer receives the broadcast ok for the holder's request but has no
    # pending entry → only the sender persists, attributed to the sender.
    station = _fm_station("fr-viewer")
    holder, viewer = _staff("fr-h"), _staff("fr-v")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        bv = _browser(viewer, station.id)
        assert (await bv.connect())[0] is True
        bh = _browser(holder, station.id)
        assert (await bh.connect())[0] is True
        await _acquire(bh)
        await bh.send_json_to(_set_frame("r7"))
        await _until(agent, lambda m: m.get("type") == "command")
        await agent.send_json_to(_ok("r7"))
        await _until(bv, lambda m: m.get("type") == "result")
        await _until(bh, lambda m: m.get("type") == "result")
        await bv.disconnect()
        await bh.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    rows = list(PersistedCapability.objects.filter(station=station))
    assert len(rows) == 1 and rows[0].updated_by_id == holder.id


@pytest.mark.django_db(transaction=True)
def test_int_slot_end_to_end_persist(control_agent_auth):
    # Real agent wire shape may carry an int slot; registry keys slots as str.
    station = _fm_station("fr-intslot", slot="1")
    staff = _staff("fr-int")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(staff, station.id)
        assert (await browser.connect())[0] is True
        await _acquire(browser)
        await browser.send_json_to(_set_frame("i1", slot=1))
        cmd = await _until(agent, lambda m: m.get("type") == "command")
        assert cmd["slot"] == 1
        await agent.send_json_to(_ok("i1"))
        await _until(browser, lambda m: m.get("type") == "result")
        await browser.disconnect()
        # Drift on an int-slot inventory → re-apply frame carries the int slot back.
        await agent.send_json_to(
            {"v": V, "type": "inventory", "slots": _inv({"filter_hpf": True}, slot=1)}
        )
        frame = await _until(agent, lambda m: m.get("type") == "command")
        await agent.disconnect()
        return frame

    frame = asyncio.run(scenario())
    row = PersistedCapability.objects.get(station=station)
    assert (row.slot, row.value) == ("1", False)
    assert frame["slot"] == 1 and frame["value"] is False


@pytest.mark.django_db(transaction=True)
def test_no_reapply_while_ptt_active(control_agent_auth):
    from apps.audio import gate as audio_gate

    station = Station.objects.create(name="fr-ptt", status="online")
    persistence.save(station, "slot0", "fm0", "filter_hpf", False, None)
    audio_gate.set_ptt(station, "slot0", "fm0", ttl=60)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(
            {"v": V, "type": "inventory", "slots": _inv({"filter_hpf": True}, slot="slot0")}
        )
        silent = await agent.receive_nothing(timeout=0.4)
        await agent.disconnect()
        return silent

    assert asyncio.run(scenario()) is True
    assert not StationAuditLog.objects.filter(
        station=station, message__contains="re-apply persisted"
    ).exists()


# -- Review round 1: persist-time role re-check against a fresh user row --------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "change",
    [
        {"membership_level": User.MembershipLevel.MEMBER},
        {"is_active": False},
    ],
)
def test_demoted_before_ok_result_does_not_persist(control_agent_auth, change):
    from channels.db import database_sync_to_async

    station = _fm_station(f"fr-demote-{list(change)[0]}")
    staff = _staff(f"fr-dm-{list(change)[0]}")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(staff, station.id)
        assert (await browser.connect())[0] is True
        await _acquire(browser)
        await browser.send_json_to(_set_frame("d1"))
        await _until(agent, lambda m: m.get("type") == "command")
        # Demote/deactivate in the DB after the send, before the agent's ok.
        await database_sync_to_async(lambda: User.objects.filter(pk=staff.pk).update(**change))()
        await agent.send_json_to(_ok("d1"))
        await _until(browser, lambda m: m.get("type") == "result")
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    assert not PersistedCapability.objects.filter(station=station).exists()
    assert not StationAuditLog.objects.filter(
        station=station, message__contains="persisted"
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_can_write_false_at_persist_time_does_not_persist(control_agent_auth, monkeypatch):
    from apps.control import capability_policy

    station = _fm_station("fr-cw-false")
    staff = _staff("fr-cw")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(staff, station.id)
        assert (await browser.connect())[0] is True
        await _acquire(browser)
        await browser.send_json_to(_set_frame("c1"))
        await _until(agent, lambda m: m.get("type") == "command")
        # Command was authorized; from now on can_write says no (persist-time re-check).
        monkeypatch.setattr(capability_policy, "can_write", lambda *a, **k: False)
        await agent.send_json_to(_ok("c1"))
        await _until(browser, lambda m: m.get("type") == "result")
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    assert not PersistedCapability.objects.filter(station=station).exists()
    assert not StationAuditLog.objects.filter(
        station=station, message__contains="persisted"
    ).exists()


# -- Review round 1: re-apply skipped during PTT is retried once PTT is released --------


def _drift_inventory():
    return {"v": V, "type": "inventory", "slots": _inv({"filter_hpf": True}, slot="slot0")}


def _telemetry():
    return {"v": V, "type": "state", "slot": "slot0", "module": "fm0", "values": {"rssi": -90}}


def _keyed_station(name, ttl=60):
    from apps.audio import gate as audio_gate

    station = Station.objects.create(name=name, status="online")
    persistence.save(station, "slot0", "fm0", "filter_hpf", False, None)
    audio_gate.set_ptt(station, "slot0", "fm0", ttl=ttl)
    return station


async def _clear_ptt(station):
    from channels.db import database_sync_to_async

    from apps.audio import gate as audio_gate

    await database_sync_to_async(audio_gate.clear_ptt)(station)


@pytest.mark.django_db(transaction=True)
def test_reapply_skipped_during_ptt_is_sent_once_after_release(control_agent_auth):
    station = _keyed_station("fr-ptt-retry")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_drift_inventory())
        assert await agent.receive_nothing(timeout=0.3)
        # Still keyed: a telemetry frame must not trigger the retry.
        await agent.send_json_to(_telemetry())
        assert await agent.receive_nothing(timeout=0.3)
        await _clear_ptt(station)
        # First agent frame after release → the pending re-apply goes out ...
        await agent.send_json_to(_telemetry())
        frame = await _until(agent, lambda m: m.get("type") == "command")
        # ... exactly once.
        await agent.send_json_to(_telemetry())
        assert await agent.receive_nothing(timeout=0.3)
        await agent.disconnect()
        return frame

    frame = asyncio.run(scenario())
    assert (frame["op"], frame["capability"], frame["value"]) == ("set", "filter_hpf", False)
    assert (
        StationAuditLog.objects.filter(
            station=station, message__contains="re-apply persisted"
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_reapply_retry_fires_from_sweep_tick_without_agent_frames(control_agent_auth, monkeypatch):
    from apps.control import constants

    monkeypatch.setattr(constants, "LOCK_SWEEP_INTERVAL_SECONDS", 0.05)
    station = _keyed_station("fr-ptt-sweep")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_drift_inventory())
        assert await agent.receive_nothing(timeout=0.3)
        await _clear_ptt(station)
        frame = await _until(agent, lambda m: m.get("type") == "command")
        assert await agent.receive_nothing(timeout=0.3)
        await agent.disconnect()
        return frame

    assert asyncio.run(scenario())["capability"] == "filter_hpf"


@pytest.mark.django_db(transaction=True)
def test_expired_ptt_is_treated_as_released(control_agent_auth):
    # ptt_active is still True in the row but the dead-man expiry has passed → not keyed.
    station = _keyed_station("fr-ptt-expired", ttl=-1)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_drift_inventory())
        frame = await _until(agent, lambda m: m.get("type") == "command")
        await agent.disconnect()
        return frame

    assert asyncio.run(scenario())["capability"] == "filter_hpf"


# -- Review round 2: deferred re-apply re-derives from CURRENT state ---------------------


@pytest.mark.django_db(transaction=True)
def test_reapply_retry_does_not_clobber_user_set_after_release(control_agent_auth):
    # Skipped while keyed (DB False vs module True). After release the holder sets the
    # cap to True: the agent answers result ok + state True. The retry must NOT replay the
    # stale keyed-time snapshot and send ``set False`` after the user's write.
    station = _keyed_station("fr2-user-set")
    staff = _staff("fr2-us")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_drift_inventory())
        assert await agent.receive_nothing(timeout=0.3)
        await _clear_ptt(station)
        browser = _browser(staff, station.id)
        assert (await browser.connect())[0] is True
        await _acquire(browser)
        await browser.send_json_to(_set_frame("u2", value=True))
        cmd = await _until(agent, lambda m: m.get("type") == "command")
        assert cmd["request_id"] == "u2"
        await agent.send_json_to(
            {"v": V, "type": "result", "request_id": "u2", "ok": True, "value": True}
        )
        await agent.send_json_to(
            {
                "v": V,
                "type": "state",
                "slot": "slot0",
                "module": "fm0",
                "values": {"filter_hpf": True},
            }
        )
        await _until(browser, lambda m: m.get("type") == "result")
        await agent.send_json_to(_telemetry())
        silent = await agent.receive_nothing(timeout=0.5)
        await browser.disconnect()
        await agent.disconnect()
        return silent

    assert asyncio.run(scenario()) is True
    assert PersistedCapability.objects.get(station=station).value is True
    assert not StationAuditLog.objects.filter(
        station=station, message__contains="re-apply persisted"
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_reapply_retry_skips_cap_with_user_set_in_flight(control_agent_auth):
    # Deterministic variant: the user's set is relayed to the agent but no result has
    # arrived (so neither DB nor last_state reflect it yet). The retry must leave that
    # capability alone instead of racing the user's write with the old persisted value.
    station = _keyed_station("fr2-inflight")
    staff = _staff("fr2-if")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_drift_inventory())
        assert await agent.receive_nothing(timeout=0.3)
        await _clear_ptt(station)
        browser = _browser(staff, station.id)
        assert (await browser.connect())[0] is True
        await _acquire(browser)
        await browser.send_json_to(_set_frame("u3", value=True))
        cmd = await _until(agent, lambda m: m.get("type") == "command")
        assert cmd["request_id"] == "u3"
        await agent.send_json_to(_telemetry())
        silent = await agent.receive_nothing(timeout=0.5)
        await browser.disconnect()
        await agent.disconnect()
        return silent

    assert asyncio.run(scenario()) is True


@pytest.mark.django_db(transaction=True)
def test_reapply_retry_uses_current_persisted_value_not_snapshot(control_agent_auth):
    # The persisted value changes between skip and retry (and the module now reports a
    # different state): the retry derives from the DB + current module state.
    from channels.db import database_sync_to_async

    station = _keyed_station("fr2-current")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_drift_inventory())  # module True vs DB False → skipped
        assert await agent.receive_nothing(timeout=0.3)
        # Module now reports False, DB now holds True (e.g. persisted by another path).
        await agent.send_json_to(
            {
                "v": V,
                "type": "state",
                "slot": "slot0",
                "module": "fm0",
                "values": {"filter_hpf": False},
            }
        )
        assert await agent.receive_nothing(timeout=0.3)
        await database_sync_to_async(persistence.save)(
            station, "slot0", "fm0", "filter_hpf", True, None
        )
        await _clear_ptt(station)
        await agent.send_json_to(_telemetry())
        frame = await _until(agent, lambda m: m.get("type") == "command")
        await agent.disconnect()
        return frame

    frame = asyncio.run(scenario())
    assert (frame["capability"], frame["value"], frame["slot"]) == ("filter_hpf", True, "slot0")


# -- unit-level: claim-before-await + no stale re-arm ---------------------------------


def _bare_agent_consumer():
    from apps.control.consumers import AgentControlConsumer

    c = AgentControlConsumer()
    c._reapply_pending = None
    c._reapply_gen = 0
    c._reapply_touched = set()
    c._wire_slots = {}
    sent = []

    async def send(text_data=None, bytes_data=None):
        sent.append(text_data)

    async def audit(station, message):
        pass

    c.send = send
    c._audit_reapply = audit
    return c, sent


_FRAME = {
    "v": 1,
    "type": "command",
    "request_id": "persist-1-x",
    "slot": "slot0",
    "module": "fm0",
    "capability": "filter_hpf",
    "op": "set",
    "value": False,
}


def test_concurrent_retries_send_exactly_one_batch():
    c, sent = _bare_agent_consumer()

    async def current(station):
        await asyncio.sleep(0)  # yield: the other retry runs while this one awaits
        await asyncio.sleep(0)
        return [dict(_FRAME), dict(_FRAME, capability="filter_lpf")]

    c._current_reapply_frames = current

    async def scenario():
        c._reapply_pending = c._reapply_gen
        await asyncio.gather(
            c._retry_pending_reapply(object()), c._retry_pending_reapply(object())
        )

    asyncio.run(scenario())
    assert len(sent) == 2  # one batch of two frames, not two batches
    assert c._reapply_pending is None


def test_sweep_retry_does_not_overwrite_newer_pending():
    c, sent = _bare_agent_consumer()
    release = None

    async def still_keyed(station):
        await release.wait()
        return None

    async def keyed_inventory(station, slots):
        return None

    c._current_reapply_frames = still_keyed
    c._reapply_frames = keyed_inventory

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        c._reapply_pending = c._reapply_gen  # old skip
        old = c._reapply_pending
        retry = asyncio.create_task(c._retry_pending_reapply(object()))
        await asyncio.sleep(0)
        # Newer inventory arrives (still keyed) while the sweep retry is in flight.
        await c._reapply_persisted(object(), [])
        newer = c._reapply_pending
        assert newer is not None and newer != old
        release.set()
        await retry
        return newer

    newer = asyncio.run(scenario())
    assert c._reapply_pending == newer
    assert sent == []


def test_sweep_retry_does_not_rearm_after_newer_inventory_resolved():
    c, sent = _bare_agent_consumer()
    release = None

    async def still_keyed(station):
        await release.wait()
        return None

    async def unkeyed_no_drift(station, slots):
        return []

    c._current_reapply_frames = still_keyed
    c._reapply_frames = unkeyed_no_drift

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        c._reapply_pending = c._reapply_gen
        retry = asyncio.create_task(c._retry_pending_reapply(object()))
        await asyncio.sleep(0)
        await c._reapply_persisted(object(), [])  # newer inventory: checked, nothing pending
        release.set()
        await retry

    asyncio.run(scenario())
    assert c._reapply_pending is None


# -- persist-time lock-holder re-check (no lock broadcast delivered) ---------------------


@pytest.mark.django_db(transaction=True)
def test_holder_changed_in_db_without_lock_event_does_not_persist(control_agent_auth):
    from channels.db import database_sync_to_async

    from apps.control.models import ControlLock

    station = _fm_station("fr2-holder")
    a, b = _staff("fr2-ha"), _staff("fr2-hb")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(a, station.id)
        assert (await browser.connect())[0] is True
        await _acquire(browser)
        await browser.send_json_to(_set_frame("h1"))
        await _until(agent, lambda m: m.get("type") == "command")
        # Lock moves to B in the DB only — no control.lock broadcast reaches A's consumer,
        # so A's _persist_pending still holds h1. Only the persist-time re-check can stop it.
        await database_sync_to_async(
            lambda: ControlLock.objects.filter(station=station).update(holder=b)
        )()
        await agent.send_json_to(_ok("h1"))
        await _until(browser, lambda m: m.get("type") == "result")
        await asyncio.sleep(0.2)
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    assert not PersistedCapability.objects.filter(station=station).exists()
    assert not StationAuditLog.objects.filter(
        station=station, message__contains="persisted"
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_reapply_retry_runs_after_the_triggering_frame_is_applied(control_agent_auth):
    # Skipped while keyed (module True vs DB False). After release the first agent frame is
    # a state frame showing the module already at the persisted value: the retry must see
    # that frame's state (runs after it is applied) and send nothing.
    station = _keyed_station("fr2-after-frame")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_drift_inventory())
        assert await agent.receive_nothing(timeout=0.3)
        await _clear_ptt(station)
        await agent.send_json_to(
            {
                "v": V,
                "type": "state",
                "slot": "slot0",
                "module": "fm0",
                "values": {"filter_hpf": False},
            }
        )
        silent = await agent.receive_nothing(timeout=0.5)
        await agent.disconnect()
        return silent

    assert asyncio.run(scenario()) is True


def test_retry_frames_carry_the_agents_int_slot_back():
    import json

    c, sent = _bare_agent_consumer()
    c._wire_slots = {"1": 1}

    async def current(station):
        return [dict(_FRAME, slot="1")]  # registry keys slots as str

    c._current_reapply_frames = current
    c._reapply_pending = c._reapply_gen
    asyncio.run(c._retry_pending_reapply(object()))
    assert [json.loads(t)["slot"] for t in sent] == [1]
