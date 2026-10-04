"""Server-side ``write_role`` enforcement at the single command choke point
(``ControlConsumer._handle_command``, spec §4a). Read-only rendering is cosmetic; this
is the real gate. Async scenarios run via asyncio.run() (no pytest-asyncio)."""

import asyncio

import pytest
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.control import capability_policy
from apps.control import consumers as control_consumers
from apps.control.models import StationModule
from apps.stations.models import Station, StationAuditLog
from config.asgi import application

DESC = [
    {"name": "filter_hpf", "kind": "setting", "type": "bool"},
    {"name": "frequency", "kind": "setting", "type": "int"},
]


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


def _setup(level, name):
    station = Station.objects.create(name=name, status="online")
    user = User.objects.create(username=f"u-{name}", membership_level=level)
    StationModule.objects.create(
        station=station, slot="slot0", module_id="fm0", type="fm", capability_descriptor=DESC
    )
    return station, user


def _cmd(capability, value=False, module="fm0", op="set", slot="slot0", rid="r1"):
    return {
        "type": "command",
        "request_id": rid,
        "slot": slot,
        "module": module,
        "capability": capability,
        "op": op,
        "value": value,
    }


def _run(station, user, frame, acquire=True):
    """Send ``frame`` as a browser (holding the lock unless acquire=False).

    Returns ("relayed", agent_frame) or ("error", browser_error_frame).
    """

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        if acquire:
            await browser.send_json_to({"type": "lock_acquire"})
            await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")
        await browser.send_json_to(frame)
        # receive_nothing (unlike a timed-out receive) does not cancel the app.
        if await agent.receive_nothing(timeout=0.5):
            outcome = ("error", await _until(browser, lambda m: m.get("type") == "error"))
        else:
            outcome = ("relayed", await agent.receive_json_from(timeout=2))
        await browser.disconnect()
        await agent.disconnect()
        return outcome

    return asyncio.run(scenario())


FORBIDDEN_STAFF = {
    "type": "error",
    "request_id": "r1",
    "error": {"code": "forbidden", "msg": "Requires role: staff"},
}


@pytest.mark.django_db(transaction=True)
def test_operator_holding_lock_cannot_set_staff_capability(control_agent_auth):
    station, member = _setup(User.MembershipLevel.MEMBER, "we1")
    kind, frame = _run(station, member, _cmd("filter_hpf"))
    assert kind == "error"
    assert frame == FORBIDDEN_STAFF
    log = StationAuditLog.objects.get(
        station=station, event_type="control_command", message__contains="denied"
    )
    assert "filter_hpf" in log.message and "staff" in log.message
    assert log.user_id == member.id


@pytest.mark.django_db(transaction=True)
def test_operator_can_still_set_operator_capability(control_agent_auth):
    station, member = _setup(User.MembershipLevel.MEMBER, "we2")
    kind, frame = _run(station, member, _cmd("frequency", 145500))
    assert kind == "relayed"
    assert frame["capability"] == "frequency" and frame["value"] == 145500


@pytest.mark.django_db(transaction=True)
def test_staff_can_set_filter_and_it_is_relayed(control_agent_auth):
    station, staff = _setup(User.MembershipLevel.STAFF, "we3")
    kind, frame = _run(station, staff, _cmd("filter_hpf"))
    assert kind == "relayed"
    assert frame == _cmd("filter_hpf")


@pytest.mark.django_db(transaction=True)
def test_unknown_module_uses_default_policy_by_name(control_agent_auth):
    # filter_hpf on a module id not in the registry → (cap, None) policy → still staff.
    station, member = _setup(User.MembershipLevel.MEMBER, "we4")
    kind, frame = _run(station, member, _cmd("filter_hpf", module="ghost9"))
    assert kind == "error" and frame == FORBIDDEN_STAFF


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("op", ["do", "toggle", None, 7])
def test_any_non_get_op_on_staff_capability_is_forbidden(control_agent_auth, op):
    station, member = _setup(User.MembershipLevel.MEMBER, f"we5-{op}")
    kind, frame = _run(station, member, _cmd("filter_hpf", op=op))
    assert kind == "error" and frame["error"]["code"] == "forbidden"


@pytest.mark.django_db(transaction=True)
def test_operator_may_read_staff_capability(control_agent_auth):
    # Reading is not writing: an operator sees the filter state (spec §4a).
    station, member = _setup(User.MembershipLevel.MEMBER, "we6")
    kind, frame = _run(station, member, _cmd("filter_hpf", value=None, op="get"))
    assert kind == "relayed" and frame["op"] == "get"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("slot", [{"x": 1}, ["slot0"], None, 3.5])
def test_malformed_slot_cannot_bypass(control_agent_auth, slot):
    station, member = _setup(User.MembershipLevel.MEMBER, f"we7-{type(slot).__name__}")
    kind, frame = _run(station, member, _cmd("filter_hpf", slot=slot))
    assert kind == "error" and frame["error"]["code"] == "forbidden"


@pytest.mark.django_db(transaction=True)
def test_role_resolution_exception_fails_closed(control_agent_auth, monkeypatch):
    station, staff = _setup(User.MembershipLevel.STAFF, "we8")

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(capability_policy, "can_write", boom)
    kind, frame = _run(station, staff, _cmd("frequency", 145500))
    assert kind == "error" and frame["error"]["code"] == "forbidden"
    assert StationAuditLog.objects.filter(station=station, message__contains="denied").exists()


@pytest.mark.django_db(transaction=True)
def test_module_lookup_exception_fails_closed(control_agent_auth, monkeypatch):
    station, staff = _setup(User.MembershipLevel.STAFF, "we9")

    def boom(self, station, slot, module):
        raise RuntimeError("db down")

    monkeypatch.setattr(control_consumers.ControlConsumer, "_lookup_module_type", boom)
    kind, frame = _run(station, staff, _cmd("filter_hpf"))
    assert kind == "error" and frame["error"]["code"] == "forbidden"


@pytest.mark.django_db(transaction=True)
def test_module_type_specific_policy_applies(control_agent_auth, monkeypatch):
    monkeypatch.setitem(
        capability_policy.POLICY, ("frequency", "fm"), capability_policy.CapabilityPolicy("admin")
    )
    station, staff = _setup(User.MembershipLevel.STAFF, "we10")
    kind, frame = _run(station, staff, _cmd("frequency", 145500))
    assert kind == "error"
    assert frame["error"] == {"code": "forbidden", "msg": "Requires role: admin"}


@pytest.mark.django_db(transaction=True)
def test_not_holding_lock_is_still_not_locked(control_agent_auth):
    station, staff = _setup(User.MembershipLevel.STAFF, "we11")
    kind, frame = _run(station, staff, _cmd("filter_hpf"), acquire=False)
    assert kind == "error" and frame["error"]["code"] == "not_locked"
