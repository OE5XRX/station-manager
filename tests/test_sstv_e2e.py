"""End-to-end SSTV (SSTV plan Task 13): the real agent ``Broker`` + ``DecoderHost`` +
``SstvDecoderPlugin`` + ``SstvTxManager`` (pty fake firmware, fake systemd units, a fake
decoder-unit socket, a fake player) talk through the real ASGI stack to the real
``AgentControlConsumer`` / ``ControlConsumer`` / SSTV ingest and TX views.

The fake unit replays what the real ``rx_unit`` emits for an ``encode()``d PD 120 picture
(computed once with the real numpy decoder). The TX download goes through the real
DeviceKey-authenticated view; what the fake player is handed is decoded back to the
picture. Async scenarios run via asyncio.run() (no pytest-asyncio)."""

import asyncio
import contextlib
import io
import os

import pytest
from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.db import connection
from django.test import Client
from django.urls import reverse
from PIL import Image

from apps.accounts.models import User
from apps.api.models import DeviceKey
from apps.control.models import AgentConnection
from apps.sstv.models import ActiveSstvRx, SstvImage, SstvTransmission
from apps.stations.models import Station
from config.asgi import application
from station_agent import sstv_tx
from station_agent.broker import Broker
from station_agent.decoders.host import DecoderHost
from station_agent.decoders.spool import Spool
from station_agent.decoders.sstv import SstvDecoderPlugin
from station_agent.slot_control import SlotControl
from station_agent.sstv.modes import MODES
from tests.conftest import _make_ed25519_keypair, device_auth_headers
from tests.fake_fw import FakeFirmware
from tests.test_agent_sstv_plugin import FakeUnit
from tests.test_agent_sstv_tx import FakePlayer
from tests.test_sstv_rx_server import _desc

np = pytest.importorskip("numpy")

pytestmark = pytest.mark.django_db(transaction=True)

RX_MODE = MODES["pd120"]  # 640 x 496
FM = {
    "schema": 1,
    "module": "fm0",
    "identity": {"type": "fm_transceiver"},
    "capabilities": _desc(),
}
SERVER = "http://testserver"
# The real plugin test's decoder tolerance (mean absolute error per channel value).
TOLERANCE = 12


def _gradient(mode):
    x = np.linspace(40, 215, mode.width)
    y = np.linspace(40, 215, mode.height)[:, None]
    shape = (mode.height, mode.width)
    return np.rint(
        np.stack(
            [
                np.broadcast_to(x, shape),
                np.broadcast_to(y, shape),
                np.broadcast_to(255 - x, shape),
            ],
            axis=2,
        )
    ).astype(np.uint8)


def _decode_pcm(pcm: bytes):
    """Run the real station decoder over s16le audio; returns its events."""
    from station_agent.sstv.decoder import StreamDecoder

    decoder = StreamDecoder(8000)
    samples = np.frombuffer(pcm, dtype="<i2")
    events = []
    for i in range(0, len(samples), 160):  # rx_unit feeds 20 ms chunks
        events += decoder.feed(samples[i : i + 160])
    return events + decoder.flush()


@pytest.fixture(scope="module")
def pd120():
    """(source picture, rx_unit NDJSON lines) of one encoded + decoded PD 120 picture."""
    from station_agent.sstv import rx_unit
    from station_agent.sstv.encoder import encode

    pic = _gradient(RX_MODE)
    events = _decode_pcm(encode(RX_MODE, pic.tobytes()) + b"\x00\x00" * 4000)
    lines = [rx_unit._line(e) for e in events]
    assert lines[0].startswith(b'{"t":"start"') and lines[-1].startswith(b'{"t":"end"')
    assert len(lines) == RX_MODE.height + 2
    return pic, lines


def _pixels(image_field):
    with image_field.open("rb") as f:
        return np.asarray(Image.open(io.BytesIO(f.read())).convert("RGB"))


class _Download:
    """What requests returns to the agent's SstvTxSession, filled from Django's test client."""

    def __init__(self, resp):
        self.status_code = resp.status_code
        self.headers = dict(resp.headers.items())
        body = b"".join(resp.streaming_content) if resp.streaming else resp.content
        resp.close()
        self.raw = io.BytesIO(body)  # the session reads it with read1()

    def close(self):
        pass


class Agent:
    """The station: a real Broker + DecoderHost + SstvTxManager behind one control WS."""

    def __init__(self, station, private_key, tmp_path):
        self.station = station
        self.fw = FakeFirmware({"fm0": FM})
        self.fw.state["fm0"].update(
            {"rx_frequency": 145.5, "tx_frequency": 145.5, "rx_tone": "none", "tx_tone": "none"}
        )
        self.unit = FakeUnit()
        self.units_calls = []
        self.host = DecoderHost(
            self,  # the unit manager (start/stop/state/stop_all below)
            {"sstv": SstvDecoderPlugin(connect=self.unit.connect)},
            lambda slot: "alsa_input.fm",
            spool=Spool(epoch="e2e"),
        )
        self.played: list[bytes] = []
        self.player = FakePlayer(run_s=0.1)
        self.downloads: list[int] = []
        self._key = private_key
        self.tx = sstv_tx.SstvTxManager(
            http_get=self._http_get,
            server_url=SERVER,
            ceiling_dbfs=lambda: -6.0,
            tx_node=lambda slot: f"alsa_output.slot{slot}",
            runtime_dir=str(tmp_path / "sstv-tx"),
            spawn=self._spawn,
            progress_interval_s=0.05,
        )
        self.comm = None
        self.broker = None
        self.seen: list[dict] = []
        self._pump_task = None
        self._fw_started = False

    # -- fake systemd units (DecoderHost unit manager) --
    def start(self, decoder, slot, env):
        self.units_calls.append(("start", decoder, slot))

    def stop(self, decoder, slot):
        self.units_calls.append(("stop", decoder, slot))

    def state(self, decoder, slot):
        return "active"

    def stop_all(self):
        pass

    # -- the station's signed HTTP client and player --
    def _http_get(self, path, headers, timeout):
        """Runs in the TX executor thread, like the real requests call."""
        extra = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in headers.items()}
        try:
            resp = Client().get(path, **extra, **device_auth_headers(self._key, self.station.pk))
            self.downloads.append(resp.status_code)
            return _Download(resp)
        finally:
            connection.close()  # this executor thread's own DB connection

    async def _spawn(self, argv):
        location = next(a for a in argv if a.startswith("location="))
        with open(location.split("=", 1)[1], "rb") as f:
            self.played.append(f.read())
        return await self.player.spawn(argv)

    def ptt(self, value):
        return sum(1 for line in self.fw.received if line == f"module fm0 do ptt {value}".encode())

    async def connect(self):
        self.fw.start()
        self._fw_started = True
        await self.unit.__aenter__()
        self.comm = WebsocketCommunicator(
            application, f"/ws/agent/control/{self.station.id}/?signature=x&timestamp=0"
        )
        assert (await self.comm.connect())[0] is True
        self.broker = Broker(
            self.comm.send_json_to,
            transport_factory=lambda path: SlotControl(path, timeout=2.0),
            decoder_host=self.host,
            sstv_tx=self.tx,
        )
        self.broker.set_inventory(
            [
                {
                    "slot": 1,
                    "control": self.fw.control_path,
                    "modules": [
                        {
                            "id": "fm0",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )
        self.host.attach(self.comm.send_json_to)
        await self.broker.emit_inventory()  # real inventory incl. features decoders + sstv
        await self.host.on_connected()
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            frame = await self.comm.receive_json_from(timeout=60)
            self.seen.append(frame)
            await self.broker.handle(frame)

    def check_pump(self):
        task = self._pump_task
        if task is not None and task.done() and not task.cancelled() and task.exception():
            raise task.exception()

    def frames(self, mtype, **match):
        return [
            f
            for f in self.seen
            if f.get("type") == mtype and all(f.get(k) == v for k, v in match.items())
        ]

    async def close(self):
        # Every step runs even if an earlier one raises (the last error propagates, earlier
        # ones are chained as __context__): a leaked pty thread or unit socket would outlive
        # the test.
        async with contextlib.AsyncExitStack() as stack:
            if self._fw_started:
                stack.callback(self.fw.stop)
            if self.unit.server is not None:
                stack.push_async_callback(self.unit.__aexit__)
            stack.push_async_callback(self.tx.shutdown)
            stack.push_async_callback(self.host.shutdown)
            if self.comm is not None:
                stack.push_async_callback(self.comm.disconnect)
            if self.broker is not None:
                stack.push_async_callback(self.broker.on_disconnect)
            self.host.detach()
            if self._pump_task is not None:
                self._pump_task.cancel()
                await asyncio.gather(self._pump_task, return_exceptions=True)


class Browser:
    """The holder's control page: collects every frame in the background."""

    def __init__(self, user, station_id):
        self.comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
        self.comm.scope["user"] = user
        self.seen: list[dict] = []
        self._task = None

    async def connect(self):
        assert (await self.comm.connect())[0] is True
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            self.seen.append(await self.comm.receive_json_from(timeout=60))

    async def send(self, msg):
        await self.comm.send_json_to(msg)

    def frames(self, mtype, **match):
        return [
            f
            for f in self.seen
            if f.get("type") == mtype and all(f.get(k) == v for k, v in match.items())
        ]

    async def close(self):
        if self._task is None:
            return  # never connected
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        await self.comm.disconnect()


async def until(pred, what, *checks, timeout=30.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        for check in checks:
            check()
        if await database_sync_to_async(pred)():
            return
        if loop.time() > end:
            raise AssertionError(f"never happened: {what}")
        await asyncio.sleep(0.02)


@pytest.fixture
def setup(control_agent_auth):
    station = Station.objects.create(name="sstv-e2e", status="online")
    user = User.objects.create(username="OE5ABC", membership_level=User.MembershipLevel.MEMBER)
    private_key, _, public_b64 = _make_ed25519_keypair()
    DeviceKey.objects.create(station=station, current_public_key=public_b64)
    return station, user, private_key


async def _session(station, user, key, tmp_path):
    agent = Agent(station, key, tmp_path)
    browser = Browser(user, station.id)
    try:
        await agent.connect()
        await until(
            lambda: (
                "sstv" in AgentConnection.objects.get(station=station, kind="control").features
            ),
            "sstv advertised",
            agent.check_pump,
        )
        await browser.connect()
        await browser.send({"type": "lock_acquire"})
        await until(
            lambda: bool(browser.frames("lock", state="held")), "lock held", agent.check_pump
        )
    except BaseException:
        await _close(agent, browser)
        raise
    return agent, browser


async def _close(agent, browser):
    try:
        await browser.close()
    finally:
        await agent.close()


async def _rx_running(agent, browser):
    await browser.send({"type": "sstv_rx_start", "request_id": "rx1", "slot": 1})
    await until(
        lambda: bool(browser.frames("sstv_state", state="running")), "rx running", agent.check_pump
    )
    (start,) = agent.frames("decoder_start", decoder="sstv")
    assert (start["slot"], start["max_runtime_s"]) == (1, 1800)
    assert agent.units_calls[-1] == ("start", "sstv", 1)
    return await agent.unit.client()  # the plugin connected to the (fake) decoder unit


def _send_lines(writer, lines):
    writer.write(b"".join(lines))
    return writer.drain()


# -- RX ------------------------------------------------------------------------------


def test_rx_picture_reaches_browser_and_gallery(setup, pd120, tmp_path):
    station, user, key = setup
    pic, lines = pd120

    async def scenario():
        agent, browser = await _session(station, user, key, tmp_path)
        try:
            writer = await _rx_running(agent, browser)
            await _send_lines(writer, lines)
            await until(
                lambda: bool(browser.frames("sstv_image")), "sstv_image shown", agent.check_pump
            )
            await until(lambda: not agent.host._spool.pending(), "image acked", agent.check_pump)
            return list(browser.seen)
        finally:
            await _close(agent, browser)

    seen = asyncio.run(scenario())
    (start,) = [f for f in seen if f["type"] == "sstv_start"]
    assert (start["mode"], start["width"], start["height"]) == ("pd120", 640, 496)
    shown_lines = [f for f in seen if f["type"] == "sstv_line"]
    assert [f["row"] for f in shown_lines] == list(range(RX_MODE.height))  # lossless in-process
    assert all(f["picture_id"] == start["picture_id"] for f in shown_lines)
    (shown,) = [f for f in seen if f["type"] == "sstv_image"]
    assert (shown["direction"], shown["mode"], shown["complete"]) == ("rx", "pd120", True)

    image = SstvImage.objects.get(station=station)
    assert str(image.pk) == shown["id"]
    assert image.picture_id == start["picture_id"]
    assert (image.direction, image.user_id, image.frequency_hz) == ("rx", user.pk, 145_500_000)
    got = _pixels(image.image)
    assert got.shape == pic.shape
    assert np.mean(np.abs(got.astype(int) - pic.astype(int))) < TOLERANCE


def test_lock_release_mid_rx_saves_the_partial_picture(setup, pd120, tmp_path):
    station, user, key = setup
    pic, lines = pd120
    received = 120  # rows 0..119 of 496: above the 10 % rule

    async def scenario():
        agent, browser = await _session(station, user, key, tmp_path)
        try:
            writer = await _rx_running(agent, browser)
            await _send_lines(writer, lines[: 1 + received])
            await until(
                lambda: len(browser.frames("sstv_line")) == received,
                "rows shown live",
                agent.check_pump,
            )
            await browser.send({"type": "lock_release"})
            await until(
                lambda: SstvImage.objects.filter(station=station).exists(),
                "partial picture stored",
                agent.check_pump,
            )
            await until(lambda: not agent.host._spool.pending(), "image acked", agent.check_pump)
            await until(
                lambda: not ActiveSstvRx.objects.filter(station=station).exists(),
                "rx ended",
                agent.check_pump,
            )
            return agent.frames("decoder_stop", decoder="sstv"), list(agent.units_calls)
        finally:
            await _close(agent, browser)

    stops, calls = asyncio.run(scenario())
    assert len(stops) == 1 and ("stop", "sstv", 1) in calls
    image = SstvImage.objects.get(station=station)
    assert (image.direction, image.complete) == ("rx", False)
    got = _pixels(image.image)
    assert got.shape == pic.shape
    diff = np.abs(got[:received].astype(int) - pic[:received].astype(int))
    assert np.mean(diff) < TOLERANCE
    assert not got[received:].any()  # rows never received are black


# -- TX ------------------------------------------------------------------------------


def test_tx_preview_send_download_play_done(setup, tmp_path):
    station, user, key = setup
    tx_mode = MODES["robot36"]
    client = Client()
    client.force_login(user)

    async def scenario():
        agent, browser = await _session(station, user, key, tmp_path)
        try:
            resp = await sync_to_async(client.post)(
                reverse("sstv:preview", args=[station.pk]),
                {"source": "test", "mode": tx_mode.key, "cs_user": "1"},
            )
            assert resp.status_code == 200, resp.content
            body = resp.json()
            assert body["warnings"] == []
            preview = await sync_to_async(client.get)(body["preview_url"])
            preview_png = b"".join(preview.streaming_content)

            await browser.send(
                {
                    "type": "sstv_tx_start",
                    "request_id": "tx1",
                    "transmission_id": body["id"],
                    "confirm_no_callsign": False,
                }
            )
            await until(
                lambda: bool(browser.frames("sstv_tx_state", state="done")),
                "tx done",
                agent.check_pump,
            )
            await until(
                lambda: bool(browser.frames("sstv_image")), "tx in gallery", agent.check_pump
            )
            return agent, body, preview_png, list(browser.seen)
        finally:
            await _close(agent, browser)

    agent, body, preview_png, seen = asyncio.run(scenario())
    tx_states = [f["state"] for f in seen if f["type"] == "sstv_tx_state"]
    assert tx_states[0] == "sending" and tx_states[-1] == "done"
    assert "keyed" in tx_states and tx_states.index("downloading") < tx_states.index("keyed")

    # The station downloaded once through the real view, keyed once and unkeyed once.
    assert agent.downloads == [200]
    assert (agent.ptt("true"), agent.ptt("false")) == (1, 1)
    assert not os.listdir(tmp_path / "sstv-tx")  # temp artefact deleted

    # What the player was handed decodes back to the preview.
    (played,) = agent.played
    events = _decode_pcm(played)
    rows = [e for e in events if type(e).__name__ == "Row"]
    assert len(rows) == tx_mode.height
    decoded = np.frombuffer(b"".join(r.rgb for r in rows), dtype=np.uint8)
    preview_px = np.asarray(Image.open(io.BytesIO(preview_png)).convert("RGB"))
    assert np.mean(np.abs(decoded.reshape(preview_px.shape).astype(int) - preview_px)) < TOLERANCE

    # Gallery row tx with the copied preview; the transmission row and its audio are gone.
    image = SstvImage.objects.get(station=station)
    (shown,) = [f for f in seen if f["type"] == "sstv_image"]
    assert shown["id"] == str(image.pk) and shown["direction"] == "tx"
    assert (image.direction, image.callsign, image.user_id) == ("tx", user.username, user.pk)
    assert (image.mode, image.frequency_hz, image.complete) == ("robot36", 145_500_000, True)
    with image.image.open("rb") as f:
        assert f.read() == preview_png
    assert not SstvTransmission.objects.filter(pk=body["id"]).exists()
