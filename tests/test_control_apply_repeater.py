"""``apply_repeater`` browser frame (repeater spec §5.3): the server resolves the repeater
into one ``apply_preset`` agent frame for the chosen slot. Async scenarios run via
asyncio.run() (no pytest-asyncio)."""

import asyncio
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.control import capability_policy
from apps.control.models import StationModule
from apps.repeaters.models import Repeater
from apps.stations.models import Station, StationAuditLog
from config.asgi import application

VHF = {"name": "vhf", "min": 134.0, "max": 174.0}
UHF = {"name": "uhf", "min": 400.0, "max": 480.0}


def _desc(*ranges, bandwidth=True):
    caps = [
        {"name": "tx_frequency", "kind": "setting", "type": "float", "ranges": list(ranges)},
        {"name": "rx_frequency", "kind": "setting", "type": "float", "ranges": list(ranges)},
        {"name": "tx_tone", "kind": "setting", "type": "string"},
        {"name": "rx_tone", "kind": "setting", "type": "string"},
    ]
    if bandwidth:
        caps.append({"name": "bandwidth", "kind": "setting", "type": "enum"})
    return caps


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


def _setup(name, level=User.MembershipLevel.MEMBER, ranges=(VHF,), bandwidth=True):
    station = Station.objects.create(name=name, status="online")
    user = User.objects.create(username=f"u-{name}", membership_level=level)
    StationModule.objects.create(
        station=station,
        slot="1",
        module_id="fm0",
        type="fm_transceiver",
        online=True,
        capability_descriptor=_desc(*ranges, bandwidth=bandwidth),
    )
    return station, user


def _rep(callsign="OE5XLL", out="145.600", inp="145.000", **kw):
    kw.setdefault("input_tone", "77.0")
    return Repeater.objects.create(
        callsign=callsign,
        output_frequency=Decimal(out),
        input_frequency=Decimal(inp),
        source="manual",
        **kw,
    )


def _frame(rep_id, slot="1", rid="r1"):
    return {"type": "apply_repeater", "request_id": rid, "repeater_id": rep_id, "slot": slot}


def _run(station, user, frame, acquire=True):
    """Send ``frame`` as a browser. Returns ("relayed", agent_frame) or ("error", frame)."""

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        if acquire:
            await browser.send_json_to({"type": "lock_acquire"})
            await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")
        await browser.send_json_to(frame)
        if await agent.receive_nothing(timeout=0.5):
            outcome = ("error", await _until(browser, lambda m: m.get("type") == "error"))
        else:
            outcome = ("relayed", await agent.receive_json_from(timeout=2))
        await browser.disconnect()
        await agent.disconnect()
        return outcome

    return asyncio.run(scenario())


def _code(frame):
    return frame.get("code") or (frame.get("error") or {}).get("code")


@pytest.mark.django_db(transaction=True)
def test_apply_requires_lock(control_agent_auth):
    station, user = _setup("ar-lock")
    rep = _rep()
    kind, frame = _run(station, user, _frame(rep.pk), acquire=False)
    assert kind == "error" and _code(frame) == "not_locked"
    assert frame["request_id"] == "r1"


@pytest.mark.django_db(transaction=True)
def test_apply_relays_preset_values(control_agent_auth):
    station, user = _setup("ar-ok")
    rep = _rep(output_tone="none", bandwidth="12.5")
    kind, frame = _run(station, user, _frame(rep.pk))
    assert kind == "relayed"
    assert frame["type"] == "apply_preset"
    assert frame["request_id"] == "r1"
    assert frame["slot"] == 1 and frame["module"] == "fm0"  # slot as int to the agent
    assert frame["values"] == {
        "tx_frequency": 145.0,
        "rx_frequency": 145.6,
        "tx_tone": "77.0",
        "rx_tone": "none",
        "bandwidth": "12.5",
    }
    assert frame["v"] == 1
    log = StationAuditLog.objects.filter(station=station, message__contains="OE5XLL")
    assert log.count() == 1
    assert "applied repeater OE5XLL" in log.get().message


@pytest.mark.django_db(transaction=True)
def test_apply_omits_unknown_bandwidth_and_unadvertised_cap(control_agent_auth):
    station, user = _setup("ar-bw", bandwidth=False)
    rep = _rep(bandwidth="25")
    kind, frame = _run(station, user, _frame(rep.pk))
    assert kind == "relayed"
    assert "bandwidth" not in frame["values"]

    station2, user2 = _setup("ar-bw2")
    rep2 = _rep(callsign="OE5NOBW", bandwidth="")
    kind, frame = _run(station2, user2, _frame(rep2.pk))
    assert kind == "relayed"
    assert "bandwidth" not in frame["values"]


@pytest.mark.django_db(transaction=True)
def test_apply_rejects_out_of_range(control_agent_auth):
    station, user = _setup("ar-range")
    rep = _rep(callsign="OE5UHF", out="438.650", inp="431.050")
    kind, frame = _run(station, user, _frame(rep.pk))
    assert kind == "error" and _code(frame) == "not_applicable"


@pytest.mark.django_db(transaction=True)
def test_apply_rejects_other_slot_band(control_agent_auth):
    """Both bands exist on the station, but not on the addressed slot."""
    station, user = _setup("ar-slot")
    StationModule.objects.create(
        station=station,
        slot="2",
        module_id="fm0",
        type="fm_transceiver",
        online=True,
        capability_descriptor=_desc(UHF),
    )
    rep = _rep(callsign="OE5UHF", out="438.650", inp="431.050")
    kind, frame = _run(station, user, _frame(rep.pk, slot="1"))
    assert kind == "error" and _code(frame) == "not_applicable"
    # The agent disconnect of the first run marked the modules offline.
    StationModule.objects.filter(station=station).update(online=True)
    kind, frame = _run(station, user, _frame(rep.pk, slot="2"))
    assert kind == "relayed" and frame["slot"] == 2


@pytest.mark.django_db(transaction=True)
def test_apply_rejects_cross_band(control_agent_auth):
    station, user = _setup("ar-cross", ranges=(VHF, UHF))
    rep = _rep(callsign="OE5X", out="438.650", inp="145.000")
    kind, frame = _run(station, user, _frame(rep.pk))
    assert kind == "error" and _code(frame) == "not_supported"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "case",
    ["inactive", "missing", "replaced", "bad_id", "bad_slot", "offline", "unknown_slot"],
)
def test_apply_rejects_unusable_target(control_agent_auth, case):
    station, user = _setup(f"ar-{case}")
    rep = _rep()
    rep_id, slot = rep.pk, "1"
    if case == "inactive":
        rep.active = False
        rep.save()
    elif case == "missing":
        rep_id = rep.pk + 999
    elif case == "replaced":
        _rep(callsign="OE5XLL-M", replaces=rep)
    elif case == "bad_id":
        rep_id = str(rep.pk)
    elif case == "bad_slot":
        slot = 1
    elif case == "offline":
        StationModule.objects.filter(station=station).update(online=False)
    elif case == "unknown_slot":
        slot = "9"
    kind, frame = _run(station, user, _frame(rep_id, slot=slot))
    assert kind == "error" and _code(frame) == "not_applicable"
    assert frame["request_id"] == "r1"


@pytest.mark.django_db(transaction=True)
def test_apply_forbidden_without_write(control_agent_auth, monkeypatch):
    station, user = _setup("ar-forbid")
    rep = _rep()
    real = capability_policy.can_write
    monkeypatch.setattr(
        capability_policy,
        "can_write",
        lambda u, cap, mt: False if cap == "rx_tone" else real(u, cap, mt),
    )
    kind, frame = _run(station, user, _frame(rep.pk))
    assert kind == "error" and _code(frame) == "forbidden"
    assert frame["request_id"] == "r1"
    assert StationAuditLog.objects.filter(station=station, message__contains="denied").exists()


@pytest.mark.django_db(transaction=True)
def test_apply_requires_control_use(control_agent_auth):
    """An applicant (control.view only) is refused before the lock check."""
    station, user = _setup("ar-appl", level=User.MembershipLevel.APPLICANT)
    rep = _rep()
    kind, frame = _run(station, user, _frame(rep.pk), acquire=False)
    assert kind == "error" and _code(frame) == "forbidden"


@pytest.mark.django_db(transaction=True)
def test_apply_result_reaches_browser_and_cancels_timeout(control_agent_auth, settings):
    settings.CONTROL_COMMAND_TIMEOUT_SECONDS = 0.3
    station, user = _setup("ar-result")
    rep = _rep()

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        await browser.send_json_to({"type": "lock_acquire"})
        await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")
        await browser.send_json_to(_frame(rep.pk, rid="r7"))
        relayed = await agent.receive_json_from(timeout=2)
        assert relayed["type"] == "apply_preset"
        await agent.send_json_to(
            {
                "v": 1,
                "type": "result",
                "request_id": "r7",
                "ok": True,
                "value": {"tx_frequency": {"ok": True}},
            }
        )
        res = await _until(browser, lambda m: m.get("type") == "result")
        assert res["request_id"] == "r7" and res["ok"] is True
        # The timeout was cancelled: no timeout error follows.
        await asyncio.sleep(0.5)
        assert await browser.receive_nothing(timeout=0.2)
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
