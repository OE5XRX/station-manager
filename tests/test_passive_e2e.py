"""End-to-end passive APRS (passive-services plan Task 18): the real agent ``Broker`` +
``DecoderHost`` + ``AprsDecoder`` (fake systemd units, fake Direwolf KISS socket) talk
through the real ASGI stack to the real ``AgentControlConsumer`` / supervisor / ingest /
coverage, and the real ``IgateWorker`` gates the stored packets to a fake APRS-IS socket.
Only the module commands are answered by the test (there is no serial line)."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.accounts.models import User
from apps.control import constants
from apps.control.models import ControlLock
from apps.passive.aprs.worker import IgateWorker
from apps.passive.models import (
    AprsPacket,
    CoverageCell,
    PassiveServiceConfig,
    PassiveServiceRun,
    StationPassiveConfig,
)
from apps.stations.models import Station
from config.asgi import application
from station_agent.broker import Broker
from station_agent.decoders.aprs import AprsDecoder
from station_agent.decoders.host import DecoderHost
from station_agent.decoders.spool import Spool
from tests.test_aprs_ax25 import _ax25
from tests.test_aprs_worker import FakeWriter, Net
from tests.test_passive_consumer import _browser, _inventory
from tests.test_passive_profiles import make_module

pytestmark = pytest.mark.django_db(transaction=True)

State = PassiveServiceRun.State
POSITION = b"!4800.00N/01400.00E-e2e"
OLD_AGE_S = 40  # older than max_gate_age_s (30)


def kiss(frame: bytes) -> bytes:
    body = frame.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc")
    return b"\xc0\x00" + body + b"\xc0"


class FakeUnits:
    def __init__(self):
        self.calls = []

    def start(self, decoder, slot, env):
        self.calls.append(("start", decoder, slot))

    def stop(self, decoder, slot):
        self.calls.append(("stop", decoder, slot))

    def state(self, decoder, slot):
        return "active"

    def stop_all(self):
        self.calls.append(("stop_all",))


class FakeDirewolf:
    """What the agent plugin would find on 127.0.0.1:810x: a KISS byte stream we feed."""

    def __init__(self):
        self.reader = asyncio.StreamReader()
        self.heard: list[float] = []  # agent-clock heard_at per frame, consumed in order

    async def connect(self, host, port):
        return self.reader, FakeWriter()

    def now(self):
        return self.heard.pop(0)


class FakeAprsPlugin(AprsDecoder):
    """The real plugin (env + KISS reader) with the TCP socket and clock injected."""

    def __init__(self, direwolf):
        super().__init__()
        self._dw = direwolf

    async def run(self, slot, emit):
        await super().run(slot, emit, connect=self._dw.connect, now=self._dw.now)


class Agent:
    """The agent side: a real Broker + DecoderHost behind one control WebSocket."""

    AGENT_NOW = 1_000.0

    def __init__(self, station_id):
        self.station_id = station_id
        self.units = FakeUnits()
        self.dw = FakeDirewolf()
        self.host = DecoderHost(
            self.units,
            {"aprs": FakeAprsPlugin(self.dw)},
            lambda slot: "alsa_input.fm",
            spool=Spool(epoch="e2e"),
            now=lambda: self.AGENT_NOW,
        )
        self.comm = None
        self.broker = None
        self.seen: list[dict] = []
        self._pump_task = None

    def feed(self, frame: bytes, heard_at: float) -> None:
        self.dw.heard.append(heard_at)
        self.dw.reader.feed_data(kiss(frame))

    async def connect(self):
        self.comm = WebsocketCommunicator(
            application, f"/ws/agent/control/{self.station_id}/?signature=x&timestamp=0"
        )
        assert (await self.comm.connect())[0] is True
        self.broker = Broker(self.comm.send_json_to, decoder_host=self.host)
        self.host.attach(self.comm.send_json_to)
        await self.comm.send_json_to(_inventory())
        await self.host.on_connected()  # decoder_snapshot, then the spool resend
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self):
        """Answer module commands (no serial line here), hand decoder frames to the Broker."""
        while True:
            frame = await self.comm.receive_json_from(timeout=30)
            self.seen.append(frame)
            if frame.get("type") == "command":
                await self.comm.send_json_to(
                    {"type": "result", "request_id": frame["request_id"], "ok": True}
                )
            else:
                await self.broker.handle(frame)

    def types(self):
        return [f["type"] for f in self.seen]

    def check_pump(self):
        """A crashed pump must fail the test loudly, not as a later 'never happened'."""
        task = self._pump_task
        if task is not None and task.done() and not task.cancelled() and task.exception():
            raise task.exception()

    async def disconnect(self):
        """Drop the WebSocket only; the host (and its decoders) live on. Idempotent."""
        self.host.detach()
        if self._pump_task is not None:
            self._pump_task.cancel()
            await asyncio.gather(self._pump_task, return_exceptions=True)
            self._pump_task = None
        if self.comm is not None:
            comm, self.comm = self.comm, None
            await comm.disconnect()

    async def close(self):
        await self.disconnect()
        await self.host.shutdown()


async def until(pred, what, agent=None, tries=200):
    for _ in range(tries):
        if agent is not None:
            agent.check_pump()
        if await database_sync_to_async(pred)():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never happened: {what}")


@pytest.fixture
def setup(control_agent_auth, monkeypatch):
    monkeypatch.setattr(constants, "LOCK_SWEEP_INTERVAL_SECONDS", 0.05)
    station = Station.objects.create(
        name="pa-e2e", status="online", latitude=Decimal("48.1"), longitude=Decimal("14.2")
    )
    make_module(station)
    PassiveServiceConfig.objects.create(
        service="aprs",
        enabled=True,
        idle_minutes=1,
        profile={"frequency": 144.8, "rx_tone": "none", "tx_tone": "none", "squelch": 0},
    )
    StationPassiveConfig.objects.create(
        station=station, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    ControlLock.objects.create(
        station=station, scope="station", released_at=timezone.now() - timedelta(minutes=2)
    )
    user = User.objects.create(username="pa-e2e-u", membership_level=User.MembershipLevel.MEMBER)
    return station, user


def run_state(station, state):
    return lambda: PassiveServiceRun.objects.filter(station=station, state=state).exists()


def test_idle_station_runs_aprs_end_to_end(setup):
    station, user = setup
    direct = _ax25("OE1ABC-9", "APRS", [], POSITION)
    digi = _ax25("OE1DEF", "APRS", ["OE5XBL-10", "WIDE2-1"], POSITION, h_bits=[True, False])
    old = _ax25("OE1GHI", "APRS", [], POSITION)
    net = Net()
    worker = IgateWorker(connect=net)

    async def scenario():
        agent = Agent(station.id)
        browsers = []
        try:
            await agent.connect()
            # idle for longer than idle_minutes: profile commands -> decoder_start -> running
            await until(run_state(station, State.RUNNING), "run running", agent)
            first = next(f for f in agent.seen if f.get("type") == "command")
            assert (first["capability"], first["value"]) == ("frequency", 144.8)
            assert agent.types().index("command") < agent.types().index("decoder_start")
            assert agent.units.calls[-1] == ("start", "aprs", 1)

            agent.feed(direct, heard_at=agent.AGENT_NOW)
            agent.feed(digi, heard_at=agent.AGENT_NOW)
            agent.feed(old, heard_at=agent.AGENT_NOW - OLD_AGE_S)
            await until(lambda: AprsPacket.objects.count() == 3, "3 packets stored", agent)
            await until(lambda: not agent.host._spool.pending(), "all 3 seqs acked", agent)
            assert sorted(f["seqs"][0] for f in agent.seen if f["type"] == "decoder_ack") == [
                1,
                2,
                3,
            ]

            # coverage: direct packets only (digipeated excluded), one cell
            (cell,) = await database_sync_to_async(list)(
                CoverageCell.objects.filter(station=station)
            )
            assert cell.count == 2

            # the worker logs in once and gates exactly the two fresh packets
            await worker.step()
            (writer,) = net.writers
            login, *rest = writer.lines
            assert login.startswith("user OE5XRX-10 pass ")
            gated = [ln for ln in rest if not ln.startswith("OE5XRX-10>")]  # drop the beacon
            assert gated == [
                "OE1ABC-9>APRS,qAR,OE5XRX-10:" + POSITION.decode(),
                "OE1DEF>APRS,OE5XBL-10*,WIDE2-1,qAR,OE5XRX-10:" + POSITION.decode(),
            ]
            rows = {
                p.source: p for p in await database_sync_to_async(list)(AprsPacket.objects.all())
            }
            assert rows["OE1GHI"].gate_skip == "stale" and rows["OE1GHI"].gated_at is None
            assert rows["OE1ABC-9"].gated_at and rows["OE1DEF"].gated_at
            before = len(writer.lines)
            await worker.step()  # nothing is gated twice
            assert len(writer.lines) == before and net.writers == [writer]

            # a member takes the radio: decoder_stop first, then the hand-over profile
            browser = _browser(user, station.id)
            browsers.append(browser)
            assert (await browser.connect())[0] is True
            await browser.send_json_to({"type": "lock_acquire"})
            await until(run_state(station, State.STOPPED), "run stopped", agent)
            types = agent.types()
            stop_at = types.index("decoder_stop")
            handover = [
                (f["capability"], f["value"])
                for f in agent.seen[stop_at:]
                if f.get("type") == "command"
            ]
            assert handover[0] == ("frequency", 145.5) and ("squelch", 1) in handover
            assert agent.units.calls[-1] == ("stop", "aprs", 1)

        finally:
            for browser in browsers:
                await browser.disconnect()
            await agent.close()
            await worker.shutdown()

    asyncio.run(scenario())
    assert PassiveServiceRun.objects.get(station=station).end_reason == "acquired"


def test_ws_drop_resend_dedup(setup):
    station, _ = setup

    async def scenario():
        agent = Agent(station.id)
        try:
            await agent.connect()
            await until(run_state(station, State.RUNNING), "run running", agent)
            run = await database_sync_to_async(PassiveServiceRun.objects.get)(station=station)

            # The WebSocket drops; the decoder keeps running and hears two packets.
            await agent.disconnect()
            await until(run_state(station, State.AGENT_OFFLINE), "run agent_offline")
            for i, who in enumerate(("OE1AAA", "OE1BBB")):
                agent.feed(_ax25(who, "APRS", [], POSITION), heard_at=agent.AGENT_NOW - i)
            await until(lambda: len(agent.host._spool.pending()) == 2, "2 frames spooled")
            # One of them had reached the server before the drop (its ack got lost).
            await database_sync_to_async(AprsPacket.objects.create)(
                station=station,
                epoch="e2e",
                seq=1,
                heard_at=timezone.now(),
                received_at=timezone.now(),
                raw_tnc2="OE1AAA>APRS:" + POSITION.decode(),
                source="OE1AAA",
                destination="APRS",
                path=[],
                packet_type="position",
            )

            # Reconnect: snapshot, spool resend, both acked, the SAME run is running again.
            mark = len(agent.seen)
            await agent.connect()
            await until(lambda: not agent.host._spool.pending(), "resent frames acked", agent)
            await until(run_state(station, State.RUNNING), "run running again", agent)
            acks = [f["seqs"][0] for f in agent.seen[mark:] if f["type"] == "decoder_ack"]
            assert sorted(acks) == [1, 2]
            assert (
                await database_sync_to_async(PassiveServiceRun.objects.get)(station=station)
            ).pk == run.pk
            after = agent.types()[mark:]  # no orphan stop, no restart, no second start
            assert "decoder_stop" not in after and "decoder_start" not in after
            assert agent.units.calls.count(("start", "aprs", 1)) == 1
        finally:
            await agent.close()

    asyncio.run(scenario())
    assert sorted(AprsPacket.objects.values_list("seq", flat=True)) == [1, 2]
