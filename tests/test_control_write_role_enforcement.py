"""Server-side capability enforcement (``control.<policy verb>``) at the single command
choke point (``ControlConsumer._handle_command``, spec 2026-10-14 §3.4). Read-only
rendering is cosmetic; this is the real gate. Denials use the unified forbidden frame
``{type:"error", code:"forbidden", message, request_id}`` (§5.2). Async scenarios run
via asyncio.run() (no pytest-asyncio)."""

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


def _forbidden(frame, rid="r1"):
    return (
        frame["type"] == "error"
        and frame["code"] == "forbidden"
        and frame.get("request_id") == rid
        and bool(frame["message"])
    )


@pytest.mark.django_db(transaction=True)
def test_operator_holding_lock_cannot_set_staff_capability(control_agent_auth):
    station, member = _setup(User.MembershipLevel.MEMBER, "we1")
    kind, frame = _run(station, member, _cmd("filter_hpf"))
    assert kind == "error"
    assert _forbidden(frame)
    log = StationAuditLog.objects.get(
        station=station, event_type="control_command", message__contains="denied"
    )
    assert "filter_hpf" in log.message and "control.manage" in log.message
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
    # filter_hpf on a module id not in the registry → (cap, None) policy → still manage.
    station, member = _setup(User.MembershipLevel.MEMBER, "we4")
    kind, frame = _run(station, member, _cmd("filter_hpf", module="ghost9"))
    assert kind == "error" and _forbidden(frame)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("op", ["do", "toggle", None, 7])
def test_any_non_get_op_on_staff_capability_is_forbidden(control_agent_auth, op):
    station, member = _setup(User.MembershipLevel.MEMBER, f"we5-{op}")
    kind, frame = _run(station, member, _cmd("filter_hpf", op=op))
    assert kind == "error" and frame["code"] == "forbidden"


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
    assert kind == "error" and frame["code"] == "forbidden"


@pytest.mark.django_db(transaction=True)
def test_role_resolution_exception_fails_closed(control_agent_auth, monkeypatch):
    station, staff = _setup(User.MembershipLevel.STAFF, "we8")

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(capability_policy, "can_write", boom)
    kind, frame = _run(station, staff, _cmd("frequency", 145500))
    assert kind == "error" and frame["code"] == "forbidden"
    assert StationAuditLog.objects.filter(station=station, message__contains="denied").exists()


@pytest.mark.django_db(transaction=True)
def test_module_lookup_exception_fails_closed(control_agent_auth, monkeypatch):
    station, staff = _setup(User.MembershipLevel.STAFF, "we9")

    def boom(self, station, slot, module):
        raise RuntimeError("db down")

    monkeypatch.setattr(control_consumers.ControlConsumer, "_lookup_module_type", boom)
    kind, frame = _run(station, staff, _cmd("filter_hpf"))
    assert kind == "error" and frame["code"] == "forbidden"


@pytest.mark.django_db(transaction=True)
def test_module_type_specific_policy_applies(control_agent_auth, monkeypatch):
    monkeypatch.setitem(
        capability_policy.POLICY,
        ("frequency", "fm"),
        capability_policy.CapabilityPolicy("manage"),
    )
    station, member = _setup(User.MembershipLevel.MEMBER, "we10")
    kind, frame = _run(station, member, _cmd("frequency", 145500))
    assert kind == "error" and _forbidden(frame)


@pytest.mark.django_db(transaction=True)
def test_not_holding_lock_is_still_not_locked(control_agent_auth):
    station, staff = _setup(User.MembershipLevel.STAFF, "we11")
    kind, frame = _run(station, staff, _cmd("filter_hpf"), acquire=False)
    assert kind == "error" and frame["error"]["code"] == "not_locked"


# -- Fix round 1 ---------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_int_slot_end_to_end_deny(control_agent_auth):
    station = Station.objects.create(name="we-int", status="online")
    member = User.objects.create(username="u-we-int", membership_level=User.MembershipLevel.MEMBER)
    StationModule.objects.create(
        station=station, slot="1", module_id="fm0", type="fm", capability_descriptor=DESC
    )
    kind, frame = _run(station, member, _cmd("filter_hpf", slot=1))
    assert kind == "error" and _forbidden(frame)
    kind, frame = _run(station, member, _cmd("frequency", 145500, slot=1))
    assert kind == "relayed" and frame["slot"] == 1


@pytest.mark.django_db(transaction=True)
def test_unregistered_module_gets_strictest_type_specific_policy(control_agent_auth, monkeypatch):
    # Only a type-specific entry exists; an unregistered module id (type unknown) must
    # not fall back to the looser default.
    monkeypatch.setitem(
        capability_policy.POLICY, ("power", "fm"), capability_policy.CapabilityPolicy("manage")
    )
    station, member = _setup(User.MembershipLevel.MEMBER, "we-strict")
    kind, frame = _run(station, member, _cmd("power", 5, module="ghost"))
    assert kind == "error" and _forbidden(frame)


@pytest.mark.django_db(transaction=True)
def test_denied_audit_message_truncates_attacker_strings(control_agent_auth):
    station, member = _setup(User.MembershipLevel.MEMBER, "we-trunc")
    long_cap = "filter_hpf" + "X" * 5000
    monkey = capability_policy.CapabilityPolicy("manage")
    capability_policy.POLICY[(long_cap, None)] = monkey
    try:
        kind, _ = _run(station, member, _cmd(long_cap, op="Y" * 5000))
    finally:
        del capability_policy.POLICY[(long_cap, None)]
    assert kind == "error"
    log = StationAuditLog.objects.get(station=station, message__contains="denied")
    assert len(log.message) < 300


# -- Review round 1: role re-checked against a FRESH user row (no stale scope user) ----


def _run_after_db_change(station, user, frame, mutate):
    """Acquire the lock as ``user``, then apply ``mutate`` to the DB row (the scope user
    instance stays stale), then send ``frame``. Returns the same outcome tuple as _run."""
    from channels.db import database_sync_to_async

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        await browser.send_json_to({"type": "lock_acquire"})
        await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")
        # Warm the cached role properties on the scope instance, then change the DB row.
        assert user.is_internal is True
        await database_sync_to_async(mutate)()
        await browser.send_json_to(frame)
        if await agent.receive_nothing(timeout=0.5):
            outcome = ("error", await _until(browser, lambda m: m.get("type") == "error"))
        else:
            outcome = ("relayed", await agent.receive_json_from(timeout=2))
        await browser.disconnect()
        await agent.disconnect()
        return outcome

    return asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_demoted_mid_connection_is_forbidden_on_next_set(control_agent_auth):
    station, staff = _setup(User.MembershipLevel.STAFF, "we-demote")

    def demote():
        User.objects.filter(pk=staff.pk).update(membership_level=User.MembershipLevel.MEMBER)

    kind, frame = _run_after_db_change(station, staff, _cmd("filter_hpf"), demote)
    assert kind == "error"
    assert _forbidden(frame)


@pytest.mark.django_db(transaction=True)
def test_deactivated_mid_connection_is_forbidden_on_next_set(control_agent_auth):
    station, staff = _setup(User.MembershipLevel.STAFF, "we-deact")

    def deactivate():
        User.objects.filter(pk=staff.pk).update(is_active=False)

    kind, frame = _run_after_db_change(station, staff, _cmd("filter_hpf"), deactivate)
    assert kind == "error"
    assert frame["code"] == "forbidden"


@pytest.mark.django_db
def test_fresh_user_denies_missing_or_inactive_row():
    # A vanished / deactivated row must deny (fail closed), never fall back to the
    # stale connect-time scope user.
    active = User.objects.create(username="fu-a", membership_level=User.MembershipLevel.STAFF)
    inactive = User.objects.create(username="fu-i", is_active=False)
    ghost = User(pk=987654, username="fu-ghost")
    assert control_consumers._fresh_user(active).pk == active.pk
    assert control_consumers._fresh_user(inactive) is None
    assert control_consumers._fresh_user(ghost) is None
    assert control_consumers._fresh_user(None) is None


@pytest.mark.django_db(transaction=True)
def test_denial_notification_failure_still_denies(control_agent_auth, monkeypatch):
    # The permission check AND the denial notification both fail (e.g. DB down): the
    # command is still refused with a generic forbidden frame, the socket survives.
    station, staff = _setup(User.MembershipLevel.STAFF, "we-notify")

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(capability_policy, "can_write", boom)
    real_allows = control_consumers.ControlConsumer.area_allows

    async def allows(self, verb, **kw):
        # lock_acquire and the receive-level control.use gate for "r1" pass normally;
        # the second "r1" check (the denial notification) blows up.
        if kw.get("request_id") == "r1":
            if getattr(self, "_seen_r1", False):
                raise RuntimeError("db down")
            self._seen_r1 = True
        return await real_allows(self, verb, **kw)

    monkeypatch.setattr(control_consumers.ControlConsumer, "area_allows", allows)
    kind, frame = _run(station, staff, _cmd("frequency", 145500))
    assert kind == "error" and _forbidden(frame)
