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
