"""SSTV receive on the server (SSTV plan Task 9): browser ``sstv_rx_start``/``sstv_rx_stop``,
agent ``decoder_output``/``decoder_state``/``decoder_snapshot`` of the ``sstv`` decoder,
``ActiveSstvRx`` lifecycle, scanner exclusivity, every lock path, idle sweep. Async
scenarios run via asyncio.run() (no pytest-asyncio)."""

import asyncio
import base64
import json
import struct
import zlib
from datetime import timedelta

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.accounts.models import User
from apps.control import lock, lock_hooks
from apps.control.models import ActiveScan, AgentConnection, ControlLock, StationModule
from apps.passive.models import PassiveServiceRun
from apps.sstv import control as sstv_control
from apps.sstv import ingest
from apps.sstv.models import ActiveSstvRx, SstvImage
from apps.stations.models import Station
from config.asgi import application
from station_agent.sstv.modes import MODES
from station_agent.sstv.png import encode_png

pytestmark = pytest.mark.django_db(transaction=True)

VHF = [{"name": "vhf", "min": 144.0, "max": 146.0}]
State = ActiveSstvRx.State


def _desc():
    return [
        {"name": "rx_frequency", "kind": "setting", "type": "float", "ranges": VHF},
        {"name": "tx_frequency", "kind": "setting", "type": "float", "ranges": VHF},
        {"name": "rx_tone", "kind": "setting", "type": "string"},
        {"name": "tx_tone", "kind": "setting", "type": "string"},
        {"name": "ptt", "kind": "action", "type": "bool"},
    ]


def setup_station(name, level=User.MembershipLevel.MEMBER):
    station = Station.objects.create(name=name, status="online")
    user = User.objects.create(username=f"u-{name}", membership_level=level)
    StationModule.objects.create(
        station=station,
        slot="1",
        module_id="fm0",
        type="fm_transceiver",
        online=True,
        capability_descriptor=_desc(),
        last_state={"rx_frequency": 145.5, "tx_frequency": 145.5},
    )
    return station, user


def _agent(station_id):
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def until(comm, pred, tries=40, timeout=2):
    for _ in range(tries):
        msg = await comm.receive_json_from(timeout=timeout)
        if pred(msg):
            return msg
    raise AssertionError("expected frame never arrived")


async def drain(comm, timeout=0.3):
    out = []
    while not await comm.receive_nothing(timeout=timeout):
        out.append(await comm.receive_json_from())
    return out


def _code(frame):
    return frame.get("code") or (frame.get("error") or {}).get("code")


@database_sync_to_async
def advertise(station, features=("sstv", "decoders")):
    AgentConnection.objects.filter(station=station).update(features=list(features))


async def connect(station, user, acquire=True, features=("sstv", "decoders")):
    agent = _agent(station.id)
    assert (await agent.connect())[0] is True
    await advertise(station, features)
    browser = _browser(user, station.id)
    assert (await browser.connect())[0] is True
    if acquire:
        await browser.send_json_to({"type": "lock_acquire"})
        await until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")
    return agent, browser


def rx_start(slot=1, rid="r1"):
    return {"type": "sstv_rx_start", "request_id": rid, "slot": slot}


def is_decoder(mtype):
    return lambda m: m.get("type") == mtype and m.get("decoder") == "sstv"


def is_state(state):
    return lambda m: m.get("type") == "sstv_state" and m.get("state") == state


def ok_result(start):
    return {"type": "result", "request_id": start["request_id"], "ok": True, "value": None}


async def started(agent, browser, slot=1):
    """Start RX like the real host answers: ok result, then the running state."""
    await browser.send_json_to(rx_start(slot))
    start = await until(agent, is_decoder("decoder_start"))
    await agent.send_json_to(ok_result(start))
    await agent.send_json_to(
        {"type": "decoder_state", "decoder": "sstv", "slot": slot, "state": "running"}
    )
    await until(browser, is_state("running"))
    return start


def rows():
    return list(ActiveSstvRx.objects.all())


@database_sync_to_async
def backdate_session(station, seconds=120):
    then = timezone.now() - timedelta(seconds=seconds)
    ActiveSstvRx.objects.filter(station=station).update(started_at=then)


async def close(*comms):
    for comm in comms:
        await comm.disconnect()


# -- PNG helpers -------------------------------------------------------------------


def png_b64(mode_key="robot36", pad=0):
    mode = MODES[mode_key]
    png = encode_png(mode.width, mode.height, bytes(mode.width * mode.height * 3))
    if pad:
        # A private ancillary chunk (ignored by decoders) blows the PNG up to a frame size.
        body = b"\0" * pad
        chunk = struct.pack(">I", len(body)) + b"prVt" + body
        chunk += struct.pack(">I", zlib.crc32(b"prVt" + body) & 0xFFFFFFFF)
        png = png[:-12] + chunk + png[-12:]  # before IEND
    return base64.b64encode(png).decode()


def image_frame(pid="a" * 32, seq=1, mode="robot36", complete=True, png=None, heard_s=1, **data):
    now = timezone.now().timestamp()
    return {
        "v": 1,
        "type": "decoder_output",
        "decoder": "sstv",
        "slot": 1,
        "epoch": "e1",
        "seq": seq,
        "kind": "sstv_image",
        "reliable": True,
        "ts": now,
        "data": {
            "picture_id": pid,
            "mode": mode,
            "complete": complete,
            "rows": 240,
            "png": png if png is not None else png_b64(mode),
            "heard_at": now - heard_s,
            "depth": 8,
            **data,
        },
    }


def is_ack(seq):
    return lambda m: m.get("type") == "decoder_ack" and m.get("seqs") == [seq]


# -- start / stop ------------------------------------------------------------------


def test_start_refused_without_lock(control_agent_auth):
    station, user = setup_station("rx-nolock")

    async def go():
        agent, browser = await connect(station, user, acquire=False)
        await browser.send_json_to(rx_start())
        err = await until(browser, lambda m: m.get("type") == "error")
        assert _code(err) == "not_locked" and err["request_id"] == "r1"
        assert not any(is_decoder("decoder_start")(m) for m in await drain(agent))
        await close(browser, agent)

    asyncio.run(go())
    assert rows() == []


def test_start_refused_without_sstv_use(control_agent_auth):
    station, user = setup_station("rx-perm", level=User.MembershipLevel.APPLICANT)

    async def go():
        agent, browser = await connect(station, user, acquire=False)
        await browser.send_json_to(rx_start())
        err = await until(browser, lambda m: m.get("type") == "error")
        assert err["code"] == "forbidden" and err["request_id"] == "r1"
        assert not any(is_decoder("decoder_start")(m) for m in await drain(agent))
        await close(browser, agent)

    asyncio.run(go())
    assert rows() == []


def test_start_refused_when_agent_does_not_advertise_sstv(control_agent_auth):
    station, user = setup_station("rx-nofeat")

    async def go():
        agent, browser = await connect(station, user, features=("decoders",))
        await browser.send_json_to(rx_start())
        err = await until(browser, lambda m: m.get("type") == "error")
        assert _code(err) == "unsupported"
        await close(browser, agent)

    asyncio.run(go())
    assert rows() == []


def test_start_refused_on_unknown_slot(control_agent_auth):
    station, user = setup_station("rx-slot")

    async def go():
        agent, browser = await connect(station, user)
        for slot in (7, "x", None, True):
            await browser.send_json_to(rx_start(slot))
            err = await until(browser, lambda m: m.get("type") == "error")
            assert _code(err) == "invalid_slot", slot
        await close(browser, agent)

    asyncio.run(go())


def test_inventory_features_are_stored_for_the_current_connection(control_agent_auth):
    station, _ = setup_station("rx-inv")

    async def go():
        agent = _agent(station.id)
        await agent.connect()
        await agent.send_json_to({"v": 1, "type": "inventory", "slots": [], "features": ["sstv"]})
        await drain(agent)
        assert await database_sync_to_async(sstv_control.supported)(station)
        await agent.disconnect()

    asyncio.run(go())
    assert not sstv_control.supported(station)  # connection gone


def test_start_sends_decoder_start_and_stops_scan(control_agent_auth):
    station, user = setup_station("rx-start")

    async def go():
        agent, browser = await connect(station, user)
        # After the agent connect (which drops a stale scan row).
        await database_sync_to_async(ActiveScan.objects.create)(
            station=station, user=user, scan_id="s" * 32, list_ids=[], listen_s=2, hang_s=5
        )
        await browser.send_json_to(rx_start())
        seen = []
        start = await until(agent, lambda m: seen.append(m) or is_decoder("decoder_start")(m))
        assert any(m.get("type") == "scan_control" and m["action"] == "stop" for m in seen)
        assert (start["slot"], start["params"], start["max_runtime_s"]) == (1, {}, 1800)
        assert start["request_id"].startswith("sstv-")
        starting = await until(browser, is_state("starting"))
        assert starting["slot"] == 1 and starting["ends_at"]
        row = await database_sync_to_async(ActiveSstvRx.objects.get)(station=station)
        assert (row.state, row.slot, row.user_id) == (State.STARTING, 1, user.pk)
        # A running state alone does not confirm (it may belong to a replaced instance).
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "running"}
        )
        await drain(browser)
        row = await database_sync_to_async(ActiveSstvRx.objects.get)(station=station)
        assert row.state == State.STARTING
        await agent.send_json_to(ok_result(start))
        await until(browser, is_state("running"))
        row = await database_sync_to_async(ActiveSstvRx.objects.get)(station=station)
        assert row.state == State.RUNNING
        await close(browser, agent)

    asyncio.run(go())
    assert not ActiveScan.objects.filter(station=station).exists()


def test_ok_result_alone_confirms_a_start(control_agent_auth):
    # A start for a decoder already running on the slot gets only the ok result.
    station, user = setup_station("rx-okres")

    async def go():
        agent, browser = await connect(station, user)
        await browser.send_json_to(rx_start())
        start = await until(agent, is_decoder("decoder_start"))
        await agent.send_json_to(
            {"type": "result", "request_id": start["request_id"], "ok": True, "value": None}
        )
        await until(browser, is_state("running"))
        await close(browser, agent)

    asyncio.run(go())


def test_sstv_results_never_reach_browsers(control_agent_auth):
    station, user = setup_station("rx-res")

    async def go():
        agent, browser = await connect(station, user)
        await agent.send_json_to({"type": "result", "request_id": "sstv-rx-stop", "ok": True})
        assert not any(m.get("type") == "result" for m in await drain(browser))
        # And a browser may not use the reserved prefix for its own commands.
        await browser.send_json_to(
            {"type": "command", "request_id": "sstv-x", "slot": 1, "module": "fm0",
             "capability": "rx_frequency", "op": "get"}
        )  # fmt: skip
        err = await until(browser, lambda m: m.get("type") == "error")
        assert _code(err) == "invalid_request_id"
        await close(browser, agent)

    asyncio.run(go())


def test_operator_stop_then_agent_stopped_removes_the_row(control_agent_auth):
    station, user = setup_station("rx-stop")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await browser.send_json_to({"type": "sstv_rx_stop", "request_id": "r2"})
        stop = await until(agent, is_decoder("decoder_stop"))
        assert stop["slot"] == 1
        idle = await until(browser, is_state("idle"))
        assert idle["detail"] == "operator"
        row = await database_sync_to_async(ActiveSstvRx.objects.get)(station=station)
        assert row.state == State.STOPPING  # final picture still attributed
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "stopped"}
        )
        await drain(browser)
        assert await database_sync_to_async(rows)() == []
        await browser.send_json_to({"type": "sstv_rx_stop", "request_id": "r3"})
        err = await until(browser, lambda m: m.get("type") == "error")
        assert _code(err) == "no_active_sstv"
        await close(browser, agent)

    asyncio.run(go())


def test_max_runtime_stop_from_agent_ends_session(control_agent_auth):
    station, user = setup_station("rx-maxrt")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "stopped",
             "detail": "max_runtime"}
        )  # fmt: skip
        idle = await until(browser, is_state("idle"))
        assert idle["detail"] == "max_runtime"
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "error",
             "detail": "no_rx_node"}
        )  # fmt: skip
        await close(browser, agent)

    asyncio.run(go())
    assert rows() == []


def test_decoder_error_reports_error_state(control_agent_auth):
    station, user = setup_station("rx-err")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "error",
             "detail": "unit failed"}
        )  # fmt: skip
        err = await until(browser, is_state("error"))
        assert err["detail"] == "unit failed"
        await close(browser, agent)

    asyncio.run(go())
    assert rows() == []


def test_restart_on_same_slot_ignores_the_late_stop(control_agent_auth):
    station, user = setup_station("rx-restart")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await browser.send_json_to({"type": "sstv_rx_stop", "request_id": "r2"})
        await browser.send_json_to(rx_start(rid="r3"))
        await until(agent, is_decoder("decoder_start"))
        # The old instance's "stopped" arrives after the new start went out.
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "stopped"}
        )
        await drain(browser)
        row = await database_sync_to_async(ActiveSstvRx.objects.get)(station=station)
        assert row.state == State.STARTING
        await close(browser, agent)

    asyncio.run(go())


# -- scanner exclusivity -----------------------------------------------------------


def test_scan_start_stops_sstv_rx(control_agent_auth):
    from tests.test_control_scan import _rep, _start

    station, user = setup_station("rx-scan")
    Station.objects.filter(pk=station.pk).update(latitude=48.0, longitude=14.0)
    station.refresh_from_db()
    _rep()

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await browser.send_json_to(_start())
        seen = []
        await until(agent, lambda m: seen.append(m) or m.get("type") == "scan_start")
        stops = [m for m in seen if is_decoder("decoder_stop")(m)]
        assert stops and seen.index(stops[0]) < len(seen) - 1  # before the scan_start
        idle = await until(browser, is_state("idle"))
        assert idle["detail"] == "scan started"
        await close(browser, agent)

    asyncio.run(go())


# -- ingest ------------------------------------------------------------------------


def test_sstv_image_stored_acked_broadcast_and_deduped(control_agent_auth):
    station, user = setup_station("rx-img")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await backdate_session(station)
        frame = image_frame(heard_s=40)
        await agent.send_json_to(frame)
        await until(agent, is_ack(1))
        shown = await until(browser, lambda m: m.get("type") == "sstv_image")
        await agent.send_json_to({**frame, "seq": 2})  # resent after a reconnect
        await until(agent, is_ack(2))
        await close(browser, agent)
        return shown

    shown = asyncio.run(go())
    (image,) = SstvImage.objects.filter(station=station)
    assert shown["id"] == str(image.pk) and shown["mode"] == "robot36" and shown["complete"]
    assert (image.direction, image.picture_id, image.user_id) == ("rx", "a" * 32, user.pk)
    assert image.frequency_hz == 145_500_000 and image.callsign == ""
    age = (timezone.now() - image.created_at).total_seconds()
    assert 35 < age < 60  # heard 40 s before it arrived (ts difference, not agent clock)
    assert image.image.read()[:8] == b"\x89PNG\r\n\x1a\n"


def test_final_picture_after_stop_keeps_the_user(control_agent_auth):
    # The agent finalises the partial picture before reporting the decoder stopped.
    station, user = setup_station("rx-final")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await browser.send_json_to({"type": "lock_release"})
        await until(agent, is_decoder("decoder_stop"))
        await agent.send_json_to(image_frame(complete=False))
        await until(agent, is_ack(1))
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "stopped"}
        )
        await drain(agent)
        await close(browser, agent)

    asyncio.run(go())
    image = SstvImage.objects.get(station=station)
    assert image.user_id == user.pk and image.complete is False
    assert rows() == []


def test_picture_from_before_the_session_is_not_attributed(control_agent_auth):
    station, user = setup_station("rx-old")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        frame = image_frame()
        frame["data"]["heard_at"] = frame["ts"] - 3600  # spooled from an earlier session
        await agent.send_json_to(frame)
        await until(agent, is_ack(1))
        await close(browser, agent)

    asyncio.run(go())
    assert SstvImage.objects.get(station=station).user is None


@pytest.mark.parametrize(
    "data",
    [
        {"png": "not base64!"},
        {"png": base64.b64encode(b"GIF89a....").decode()},
        {"mode": "wraase"},
        {"picture_id": "../../x"},
        {"complete": "yes"},
        {"png": png_b64("martin1")},  # 320x256 PNG claimed as Robot 36
    ],
)
def test_bad_pictures_are_acked_but_not_stored(control_agent_auth, data):
    station, user = setup_station("rx-bad")

    async def go():
        agent, browser = await connect(station, user)
        frame = image_frame()
        frame["data"].update(data)
        await agent.send_json_to(frame)
        await until(agent, is_ack(1))
        await close(browser, agent)

    asyncio.run(go())
    assert not SstvImage.objects.exists()


def test_malformed_seq_is_not_acked():
    assert ingest.ingest_output(1, {**image_frame(), "seq": -1}, recv_now=timezone.now()) is False
    assert (
        ingest.ingest_output(1, {**image_frame(), "epoch": ""}, recv_now=timezone.now()) is False
    )


def test_large_picture_frame_is_accepted(control_agent_auth):
    # The agent keeps the PNG's base64 under 900 KB so the whole frame stays below daphne's
    # default websocket_max_message_size (1 MiB). The server takes such a frame.
    station, user = setup_station("rx-big")
    big = png_b64("pd180", pad=670_000)
    assert 880_000 < len(big) <= 900_000
    frame = image_frame(mode="pd180", png=big)
    assert len(json.dumps(frame)) < 1024 * 1024

    async def go():
        agent, browser = await connect(station, user)
        await agent.send_json_to(frame)
        await until(agent, is_ack(1))
        await close(browser, agent)

    asyncio.run(go())
    assert SstvImage.objects.get(station=station).mode == "pd180"


def test_oversized_picture_is_dropped():
    station, _ = setup_station("rx-huge")
    frame = image_frame(png="A" * (ingest.MAX_PNG_B64 + 4))
    assert ingest.ingest_output(station.pk, frame, recv_now=timezone.now()) is True
    assert not SstvImage.objects.exists()


def test_live_start_and_lines_reach_the_browser_unacked(control_agent_auth):
    station, user = setup_station("rx-live")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        base = {"v": 1, "type": "decoder_output", "decoder": "sstv", "slot": 1, "epoch": "e1"}
        await agent.send_json_to(
            {**base, "seq": 5, "kind": "sstv_start", "reliable": True, "ts": 1.0,
             "data": {"picture_id": "p1", "mode": "pd120", "width": 1, "height": 1}}
        )  # fmt: skip
        await until(agent, is_ack(5))
        start = await until(browser, lambda m: m.get("type") == "sstv_start")
        assert (start["mode"], start["width"], start["height"]) == ("pd120", 640, 496)
        line = {"picture_id": "p1", "row": 3, "rgb": "AAAA"}
        await agent.send_json_to(
            {**base, "seq": 0, "kind": "sstv_line", "reliable": False, "ts": 1.0, "data": line}
        )
        got = await until(browser, lambda m: m.get("type") == "sstv_line")
        assert {k: got[k] for k in line} == line
        for bad in ({**line, "row": -1}, {**line, "rgb": "A" * 5000}, {**line, "row": "3"}):
            await agent.send_json_to(
                {**base, "seq": 0, "kind": "sstv_line", "reliable": False, "data": bad}
            )
        assert not any(m.get("type") == "decoder_ack" for m in await drain(agent))
        assert not any(m.get("type") == "sstv_line" for m in await drain(browser))
        await close(browser, agent)

    asyncio.run(go())
    assert not SstvImage.objects.exists()


# -- lock paths --------------------------------------------------------------------


def _running(station, user):
    return ActiveSstvRx.objects.create(station=station, slot=1, user=user, state=State.RUNNING)


@pytest.mark.parametrize("reason", ["released", "revoked", "expired"])
def test_release_paths_stop_rx(control_agent_auth, reason):
    # "expired" = idle sweep and holder-disconnect grace (both go through the sweep).
    station, user = setup_station(f"rx-rel-{reason}")

    async def go():
        agent = _agent(station.id)
        await agent.connect()
        await database_sync_to_async(_running)(station, user)
        await lock_hooks.on_lock_released(station, reason)
        stop = await until(agent, is_decoder("decoder_stop"))
        assert stop["slot"] == 1
        await agent.disconnect()

    asyncio.run(go())
    assert not ActiveSstvRx.objects.filter(state__in=sstv_control._LIVE_STATES).exists()


@pytest.mark.parametrize("reason", ["transferred", "preempted"])
def test_holder_change_stops_rx(control_agent_auth, reason):
    station, user = setup_station(f"rx-acq-{reason}")
    other = User.objects.create(username=f"o-{reason}", membership_level="staff")

    async def go():
        agent = _agent(station.id)
        await agent.connect()
        await database_sync_to_async(_running)(station, user)
        await lock_hooks.on_lock_acquired(station, other, reason, previous_holder_lost=True)
        await until(agent, is_decoder("decoder_stop"))
        await agent.disconnect()

    asyncio.run(go())
    assert not ActiveSstvRx.objects.filter(state__in=sstv_control._LIVE_STATES).exists()


def test_preempting_oneself_keeps_ones_rx():
    station, user = setup_station("rx-self")
    _running(station, user)
    asyncio.run(lock_hooks.on_lock_acquired(station, user, "preempted", previous_holder_lost=True))
    assert ActiveSstvRx.objects.get(station=station).state == State.RUNNING


def test_plain_grant_does_not_touch_rx():
    station, user = setup_station("rx-grant")
    _running(station, user)
    asyncio.run(lock_hooks.on_lock_acquired(station, user, "acquired", previous_holder_lost=False))
    assert ActiveSstvRx.objects.get(station=station).state == State.RUNNING


def test_lock_release_from_browser_stops_rx(control_agent_auth):
    station, user = setup_station("rx-brel")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await browser.send_json_to({"type": "lock_release"})
        await until(agent, is_decoder("decoder_stop"))
        idle = await until(browser, is_state("idle"))
        assert idle["detail"] == "lock released"
        await close(browser, agent)

    asyncio.run(go())


def test_holder_disconnect_grace_expiry_stops_rx(control_agent_auth, monkeypatch):
    from apps.control import constants

    monkeypatch.setattr(constants, "LOCK_SWEEP_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(constants, "RECONNECT_GRACE_SECONDS", 0)
    station, user = setup_station("rx-grace")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await browser.disconnect()  # holder's browser gone -> grace -> sweep release
        await until(agent, is_decoder("decoder_stop"), tries=200)
        await agent.disconnect()

    asyncio.run(go())
    assert ControlLock.objects.get(station=station).holder is None


def test_agent_disconnect_ends_rx_and_tells_browsers(control_agent_auth):
    station, user = setup_station("rx-agentgone")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await agent.disconnect()
        idle = await until(browser, is_state("idle"))
        assert idle["detail"] == "agent_disconnect"
        await browser.disconnect()

    asyncio.run(go())
    assert not ActiveSstvRx.objects.filter(state__in=sstv_control._LIVE_STATES).exists()


def test_reconnected_agent_drops_a_stale_session(control_agent_auth):
    station, user = setup_station("rx-recon")
    _running(station, user)

    async def go():
        agent = _agent(station.id)
        await agent.connect()
        await agent.disconnect()

    asyncio.run(go())
    assert ActiveSstvRx.objects.get(station=station).state == State.STOPPING


def test_snapshot_stops_sstv_decoders_nobody_started(control_agent_auth):
    station, _ = setup_station("rx-orphan")

    async def go():
        agent = _agent(station.id)
        await agent.connect()
        await agent.send_json_to(
            {"type": "decoder_snapshot", "decoders": [
                {"decoder": "sstv", "slot": 1, "state": "running"},
                {"decoder": "aprs", "slot": 2, "state": "running"},
            ]}
        )  # fmt: skip
        stop = await until(agent, is_decoder("decoder_stop"))
        assert stop["slot"] == 1
        await agent.disconnect()

    asyncio.run(go())


def test_browser_snapshot_shows_running_rx(control_agent_auth):
    station, user = setup_station("rx-snap")
    _running(station, user)

    async def go():
        browser = _browser(user, station.id)
        await browser.connect()
        state = await until(browser, lambda m: m.get("type") == "sstv_state")
        await browser.disconnect()
        return state

    state = asyncio.run(go())
    assert state["state"] == "running" and state["slot"] == 1


# -- idle sweep --------------------------------------------------------------------


def _held(station, user, idle_s):
    lock.acquire(station, user)
    then = timezone.now() - timedelta(seconds=idle_s)
    ControlLock.objects.filter(station=station).update(last_activity=then)


def test_running_rx_keeps_the_lock_and_refreshes_activity():
    station, user = setup_station("rx-sweep")
    _held(station, user, 600)
    _running(station, user)
    assert lock.sweep_lock(station, timezone.now(), idle_seconds=300) is False
    fresh = ControlLock.objects.get(station=station).last_activity
    assert (timezone.now() - fresh).total_seconds() < 5


def test_idle_lock_without_sstv_is_swept():
    station, user = setup_station("rx-sweep2")
    _held(station, user, 600)
    ActiveSstvRx.objects.create(station=station, slot=1, user=user, state=State.STOPPING)
    assert lock.sweep_lock(station, timezone.now(), idle_seconds=300) is True


def test_unconfirmed_start_counts_only_for_the_grace():
    station, user = setup_station("rx-grace2")
    row = ActiveSstvRx.objects.create(station=station, slot=1, user=user, state=State.STARTING)
    assert sstv_control.counts_as_activity(station)
    ActiveSstvRx.objects.filter(pk=row.pk).update(
        started_at=timezone.now() - timedelta(seconds=sstv_control.START_GRACE_S + 1)
    )
    assert not sstv_control.counts_as_activity(station)


# -- passive supervisor ------------------------------------------------------------


@pytest.fixture
def aprs_env(station):
    """A supervisor with an eligible APRS config (as ``tests.test_passive_supervisor.env``)."""
    from apps.passive.models import HandoverProfile, PassiveServiceConfig, StationPassiveConfig
    from apps.passive.supervisor import PassiveSupervisor
    from tests.test_passive_profiles import make_module
    from tests.test_passive_supervisor import Clock, FakeAgent

    PassiveServiceConfig.objects.create(
        service="aprs",
        enabled=True,
        idle_minutes=10,
        profile={"frequency": 144.8, "bandwidth": "25", "rx_tone": "none", "tx_tone": "none",
                 "squelch": 0},
    )  # fmt: skip
    HandoverProfile.get()
    StationPassiveConfig.objects.create(
        station=station, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    make_module(station)
    agent, clock = FakeAgent(), Clock()
    sup = PassiveSupervisor(
        station.pk, agent.send, agent.broadcast, now=clock, command_timeout=0.5
    )
    agent.sup = sup
    sup.set_agent_features(["decoders"])
    sup._reconciled = True
    return station, sup, agent, clock


def test_passive_supervisor_ignores_sstv_decoder_state(aprs_env):
    from tests.test_passive_supervisor import started as aprs_started

    station, sup, agent, _ = aprs_env

    async def go():
        run = await aprs_started(station, sup, agent)
        for state in ("stopped", "error", "running"):
            await sup.on_decoder_state({"decoder": "sstv", "slot": 1, "state": state})
        await sup.drain()
        return run

    run = asyncio.run(go())
    run.refresh_from_db()
    assert run.state == PassiveServiceRun.State.RUNNING and run.end_reason == ""


def test_passive_supervisor_snapshot_ignores_sstv_entries(aprs_env):
    from tests.test_passive_supervisor import run_of
    from tests.test_passive_supervisor import started as aprs_started

    station, sup, agent, _ = aprs_env

    async def go():
        await aprs_started(station, sup, agent)
        sup._reconciled = False  # a reconnect: the next snapshot reconciles again
        await sup.on_snapshot(
            {"decoders": [
                {"decoder": "aprs", "slot": 1, "state": "running"},
                {"decoder": "sstv", "slot": 2, "state": "running"},
            ]}
        )  # fmt: skip
        await sup.drain()

    asyncio.run(go())
    assert run_of(station).state == PassiveServiceRun.State.RUNNING
    assert agent.sent("decoder_stop") == []


def test_passive_supervisor_snapshot_with_only_sstv_is_no_aprs_run(aprs_env):
    # Only an SSTV decoder on the APRS slot: the APRS run is gone (agent restarted), and
    # the SSTV decoder is never stopped as an APRS orphan.
    from tests.test_passive_supervisor import run_of
    from tests.test_passive_supervisor import started as aprs_started

    station, sup, agent, _ = aprs_env

    async def go():
        await aprs_started(station, sup, agent)
        sup._reconciled = False
        await sup.on_snapshot({"decoders": [{"decoder": "sstv", "slot": 1, "state": "running"}]})
        await sup.drain()

    asyncio.run(go())
    assert run_of(station).end_reason == "agent_restarted"
    assert all(f["decoder"] != "sstv" for f in agent.sent("decoder_stop"))


def test_passive_supervisor_never_stops_an_sstv_decoder_as_orphan(aprs_env):
    station, sup, agent, _ = aprs_env

    async def go():
        sup._reconciled = False
        await sup.on_snapshot({"decoders": [{"decoder": "sstv", "slot": 1, "state": "running"}]})
        await sup.drain()

    asyncio.run(go())
    assert agent.sent("decoder_stop") == []
    assert not PassiveServiceRun.objects.exists()


# -- review round 1 ------------------------------------------------------------------


def test_start_refused_only_by_result_ends_the_session(control_agent_auth):
    station, user = setup_station("rx-refres")

    async def go():
        agent, browser = await connect(station, user)
        await browser.send_json_to(rx_start())
        start = await until(agent, is_decoder("decoder_start"))
        await agent.send_json_to(
            {"type": "result", "request_id": start["request_id"], "ok": False,
             "error": {"code": "unsupported", "msg": ""}}
        )  # fmt: skip
        err = await until(browser, is_state("error"))
        assert err["detail"] == "unsupported"
        await close(browser, agent)

    asyncio.run(go())
    assert rows() == []


def test_start_on_another_slot_stops_the_old_one(control_agent_auth):
    station, user = setup_station("rx-other")
    StationModule.objects.create(
        station=station, slot="2", module_id="fm1", type="fm_transceiver", online=True,
        capability_descriptor=_desc(), last_state={"rx_frequency": 433.5},
    )  # fmt: skip

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await browser.send_json_to(rx_start(slot=2, rid="r2"))
        seen = []
        start = await until(agent, lambda m: seen.append(m) or is_decoder("decoder_start")(m))
        stops = [m for m in seen if is_decoder("decoder_stop")(m)]
        assert [m["slot"] for m in stops] == [1] and start["slot"] == 2
        await close(browser, agent)

    asyncio.run(go())


def test_stale_running_session_stops_counting_as_activity():
    station, user = setup_station("rx-stale")
    row = _running(station, user)
    then = timezone.now() - timedelta(seconds=sstv_control.RX_MAX_S + 1)
    ActiveSstvRx.objects.filter(pk=row.pk).update(started_at=then)
    assert not sstv_control.counts_as_activity(station)


def test_agent_detail_is_scrubbed_in_the_audit():
    from apps.stations.models import StationAuditLog

    station, user = setup_station("rx-audit")
    _running(station, user)
    sstv_control.on_decoder_state(
        station,
        {"slot": 1, "state": "error", "detail": "reader: connect to 10.1.2.3 <x@y.at> failed"},
    )
    msg = StationAuditLog.objects.filter(station=station).latest("pk").message
    assert "10.1.2.3" not in msg and "x@y.at" not in msg and "<" not in msg


# -- review round 2 ------------------------------------------------------------------


def test_late_result_of_a_replaced_start_is_ignored(control_agent_auth):
    station, user = setup_station("rx-late")
    StationModule.objects.create(
        station=station, slot="2", module_id="fm1", type="fm_transceiver", online=True,
        capability_descriptor=_desc(), last_state={},
    )  # fmt: skip

    async def go():
        agent, browser = await connect(station, user)
        await browser.send_json_to(rx_start(slot=1))
        first = await until(agent, is_decoder("decoder_start"))
        await browser.send_json_to(rx_start(slot=2, rid="r2"))
        second = await until(agent, is_decoder("decoder_start"))
        assert first["request_id"] != second["request_id"]
        # The refusal of the first (slot 1) start arrives late: slot 2 stays.
        await agent.send_json_to(
            {"type": "result", "request_id": first["request_id"], "ok": False,
             "error": {"code": "no_rx_node", "msg": ""}}
        )  # fmt: skip
        await drain(browser)
        row = await database_sync_to_async(ActiveSstvRx.objects.get)(station=station)
        assert (row.slot, row.state) == (2, State.STARTING)
        await agent.send_json_to(
            {"type": "result", "request_id": second["request_id"], "ok": True, "value": None}
        )
        await until(browser, is_state("running"))
        await close(browser, agent)

    asyncio.run(go())


def test_unowned_running_decoder_is_stopped(control_agent_auth):
    station, user = setup_station("rx-unowned")

    async def go():
        agent = _agent(station.id)
        await agent.connect()
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "running"}
        )
        stop = await until(agent, is_decoder("decoder_stop"))
        assert stop["slot"] == 1
        await agent.disconnect()

    asyncio.run(go())


def test_same_slot_restart_restarts_the_decoder(control_agent_auth):
    station, user = setup_station("rx-same")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        await browser.send_json_to(rx_start(rid="r2"))
        seen = []
        await until(agent, lambda m: seen.append(m) or is_decoder("decoder_start")(m))
        assert any(is_decoder("decoder_stop")(m) and m["slot"] == 1 for m in seen)
        await close(browser, agent)

    asyncio.run(go())


@pytest.mark.parametrize("slot", ["01", "²", "1 ", True, -1, 1.0])
def test_odd_slots_are_rejected(slot):
    assert sstv_control.slot_key(slot) is None


def test_snapshot_with_odd_slots_does_not_break_or_stop_own_decoder():
    station, user = setup_station("rx-oddsnap")
    _running(station, user)
    sstv_control.on_snapshot(
        station,
        [
            {"decoder": "sstv", "slot": 1, "state": "running"},
            {"decoder": "sstv", "slot": "01", "state": "running"},
            {"decoder": "sstv", "slot": "²", "state": "running"},
        ],
    )
    assert ActiveSstvRx.objects.get(station=station).state == State.RUNNING


# -- review round 3 ------------------------------------------------------------------


def test_same_slot_restart_survives_the_old_instance_frames(control_agent_auth):
    # Restart while the previous start is still pending: the host answers
    # result(A) running(A) stopped(A) starting(B) result(B) running(B).
    station, user = setup_station("rx-replay")

    async def go():
        agent, browser = await connect(station, user)
        await browser.send_json_to(rx_start())
        a = await until(agent, is_decoder("decoder_start"))
        await browser.send_json_to(rx_start(rid="r2"))
        b = await until(agent, is_decoder("decoder_start"))
        state = {"type": "decoder_state", "decoder": "sstv", "slot": 1}
        for frame in (
            ok_result(a),
            {**state, "state": "running"},
            {**state, "state": "stopped", "detail": "requested"},
            {**state, "state": "error", "detail": "late"},
            {**state, "state": "starting"},
            ok_result(b),
            {**state, "state": "running"},
        ):
            await agent.send_json_to(frame)
        await until(browser, is_state("running"))
        await drain(browser)
        row = await database_sync_to_async(ActiveSstvRx.objects.get)(station=station)
        assert (row.state, row.start_request) == (State.RUNNING, b["request_id"])
        await close(browser, agent)

    asyncio.run(go())


@pytest.mark.parametrize(
    "detail, expected",
    [
        ("player_failed rc=1", "player_failed rc=1"),
        ("reader: connect to 10.1.2.3 failed", "reader"),
        ("unit failed: Job for x failed", "unit failed"),
        ("max_runtime", "max_runtime"),
        ("x" * 50 + " user@example.org", "error"),
        ("connect 10.1.2<.3 failed", "error"),
        ("connect 10.1.2.3", "error"),
        ("mail peter.b<@example.com x", "error"),
        ("", "error"),
    ],
)
def test_audit_detail_keeps_only_the_leading_code(detail, expected):
    assert sstv_control.audit_detail(detail) == expected
