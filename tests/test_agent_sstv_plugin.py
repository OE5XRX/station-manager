"""SSTV decoder plugin (agent side) against a fake unit speaking the rx_unit protocol."""

import asyncio
import base64
import json
import zlib

import pytest

from station_agent import decoders
from station_agent.broker import Broker
from station_agent.decoders import sstv as sstv_mod
from station_agent.decoders.host import DecoderHost
from station_agent.decoders.spool import Spool
from station_agent.decoders.sstv import SstvDecoderPlugin
from station_agent.sstv.modes import MODES

MODE = MODES["martin1"]  # 320 x 256
W, H = MODE.width, MODE.height


def run(coro):
    return asyncio.run(coro)


def _row(i: int, salt: int = 0) -> bytes:
    return bytes(((i + salt + x) % 256) for x in range(W * 3))


def _start(mode=MODE) -> dict:
    return {"t": "start", "mode": mode.key, "width": mode.width, "height": mode.height}


def _row_line(i: int, salt: int = 0) -> dict:
    return {"t": "row", "i": i, "rgb": base64.b64encode(_row(i, salt)).decode("ascii")}


def _end(complete: bool, rows: int) -> dict:
    return {"t": "end", "complete": complete, "rows": rows}


def _png_rows(png: bytes) -> list[bytes]:
    """Decode our own PNG writer's output (filter 0) into rows; checks the size."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(png):
        n = int.from_bytes(png[pos : pos + 4], "big")
        kind = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + n]
        if kind == b"IHDR":
            size = (int.from_bytes(data[:4], "big"), int.from_bytes(data[4:8], "big"))
        elif kind == b"IDAT":
            idat += data
        pos += 12 + n
    w, h = size
    raw = zlib.decompress(idat)
    stride = w * 3 + 1
    assert len(raw) == stride * h
    return [raw[y * stride + 1 : (y + 1) * stride] for y in range(h)], (w, h)


class FakeUnit:
    """TCP server on an ephemeral port; each accepted client gets the queued lines."""

    def __init__(self):
        self.sessions: asyncio.Queue = asyncio.Queue()
        self.server = None
        self.port = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        self.server.abort_clients()
        await self.server.wait_closed()

    async def _client(self, reader, writer):
        await self.sessions.put(writer)

    async def client(self, timeout=3.0) -> asyncio.StreamWriter:
        return await asyncio.wait_for(self.sessions.get(), timeout)

    async def connect(self, host, port, **kw):
        assert host == "127.0.0.1"
        return await asyncio.open_connection(host, self.port, **kw)


async def send(writer, *msgs):
    writer.write(b"".join(json.dumps(m).encode() + b"\n" for m in msgs))
    await writer.drain()


class Emits:
    def __init__(self):
        self.items = []

    async def __call__(self, kind, reliable, data):
        self.items.append((kind, reliable, data))

    def of(self, kind):
        return [d for k, _, d in self.items if k == kind]


async def wait_for(cond, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _plugin(unit, **kw):
    return SstvDecoderPlugin(connect=unit.connect, now=lambda: 123.0, **kw)


def test_port_base_matches_rx_unit():
    pytest.importorskip("numpy")
    from station_agent.sstv import rx_unit

    assert sstv_mod.SSTV_PORT_BASE == rx_unit.SSTV_PORT_BASE


def test_env():
    env = SstvDecoderPlugin().env(2, {}, "rx.node")
    assert env == {
        "OE5XRX_DECODER": "sstv",
        "OE5XRX_SLOT": "2",
        "OE5XRX_RX_NODE": "rx.node",
        "OE5XRX_SSTV_PORT": "8202",
        "OE5XRX_SAMPLE_RATE": "8000",
    }
    assert SstvDecoderPlugin.stop_on_link_loss is True


def test_full_picture():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(H)], _end(True, H))
            await wait_for(lambda: emits.of("sstv_image"))
            task.cancel()
            return emits

    emits = run(go())
    kinds = [(k, r) for k, r, _ in emits.items]
    assert kinds[0] == ("sstv_start", True)
    assert kinds[1 : H + 1] == [("sstv_line", False)] * H
    assert kinds[H + 1 :] == [("sstv_image", True)]
    start = emits.of("sstv_start")[0]
    assert start["mode"] == "martin1" and (start["width"], start["height"]) == (W, H)
    assert len(start["picture_id"]) == 32
    lines = emits.of("sstv_line")
    assert [d["row"] for d in lines] == list(range(H))
    assert all(d["picture_id"] == start["picture_id"] for d in lines)
    assert lines[5]["rgb"] == _row_line(5)["rgb"]
    image = emits.of("sstv_image")[0]
    assert image["picture_id"] == start["picture_id"]
    assert image["mode"] == "martin1"
    assert image["complete"] is True and image["rows"] == H
    assert image["heard_at"] == 123.0
    rows, size = _png_rows(base64.b64decode(image["png"]))
    assert size == (W, H)
    assert rows == [_row(i) for i in range(H)]


def test_end_incomplete_keeps_rows_and_10_percent_rule():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            # Lost picture with 100 rows -> saved incomplete; then one with 20 rows -> dropped.
            await send(w, _start(), *[_row_line(i) for i in range(100)], _end(False, 100))
            await send(w, _start(), *[_row_line(i) for i in range(20)], _end(False, 20))
            await send(w, _start(), _row_line(0))  # marker: everything before is handled
            await wait_for(lambda: len(emits.of("sstv_start")) == 3)
            task.cancel()
            return emits

    emits = run(go())
    images = emits.of("sstv_image")
    assert len(images) == 1
    assert images[0]["complete"] is False and images[0]["rows"] == 100
    rows, _ = _png_rows(base64.b64decode(images[0]["png"]))
    assert rows[99] == _row(99) and rows[100] == bytes(W * 3)


def test_end_complete_with_missing_rows_is_incomplete():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(H - 1)], _end(True, H))
            await wait_for(lambda: emits.of("sstv_image"))
            task.cancel()
            return emits.of("sstv_image")[0]

    image = run(go())
    assert image["complete"] is False and image["rows"] == H - 1


def test_invalid_lines_are_ignored():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            w.write(b"not json\n[1,2]\n")
            await send(
                w,
                {"t": "row", "i": 0, "rgb": _row_line(0)["rgb"]},  # row before start
                {"t": "start", "mode": "nope", "width": W, "height": H},
                {"t": "start", "mode": "martin1", "width": 1, "height": H},
                _start(),
                {"t": "row", "i": H, "rgb": _row_line(0)["rgb"]},  # out of range
                {"t": "row", "i": True, "rgb": _row_line(0)["rgb"]},
                {"t": "row", "i": 1, "rgb": "!!!"},  # bad base64
                {"t": "row", "i": 1, "rgb": base64.b64encode(b"short").decode()},
                _row_line(2),
            )
            await wait_for(lambda: emits.of("sstv_line"))
            await asyncio.sleep(0.05)
            task.cancel()
            return emits

    emits = run(go())
    assert len(emits.of("sstv_start")) == 1
    assert [d["row"] for d in emits.of("sstv_line")] == [2]


def test_replay_after_reconnect_keeps_picture():
    """The unit replays start + all rows to a new client: still one picture."""

    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit, sleep=lambda s: asyncio.sleep(0)), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(50)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 50)
            w.transport.abort()  # connection hiccup
            w2 = await unit.client()
            await send(w2, _start(), *[_row_line(i) for i in range(80)])
            await send(w2, *[_row_line(i) for i in range(80, H)], _end(True, H))
            await wait_for(lambda: emits.of("sstv_image"))
            task.cancel()
            return emits

    emits = run(go())
    assert len(emits.of("sstv_start")) == 1
    # Replayed rows already shown are not re-emitted.
    assert [d["row"] for d in emits.of("sstv_line")] == list(range(H))
    images = emits.of("sstv_image")
    assert len(images) == 1 and images[0]["complete"] is True
    assert images[0]["picture_id"] == emits.of("sstv_start")[0]["picture_id"]


def test_replay_of_a_different_picture_splits():
    """Ours ended unseen while disconnected and a new one of the same mode began."""

    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit, sleep=lambda s: asyncio.sleep(0)), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(60)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 60)
            w.transport.abort()
            w2 = await unit.client()
            await send(w2, _start(), *[_row_line(i, salt=7) for i in range(H)], _end(True, H))
            await wait_for(lambda: len(emits.of("sstv_image")) == 2)
            task.cancel()
            return emits

    emits = run(go())
    starts = emits.of("sstv_start")
    assert len(starts) == 2 and starts[0]["picture_id"] != starts[1]["picture_id"]
    first, second = emits.of("sstv_image")
    assert first["picture_id"] == starts[0]["picture_id"]
    assert first["complete"] is False and first["rows"] == 60
    assert second["picture_id"] == starts[1]["picture_id"]
    assert second["complete"] is True
    rows, _ = _png_rows(base64.b64decode(second["png"]))
    assert rows == [_row(i, salt=7) for i in range(H)]


def test_new_start_while_open_finalizes_old():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(40)], _start())
            await wait_for(lambda: len(emits.of("sstv_start")) == 2)
            task.cancel()
            return emits

    emits = run(go())
    images = emits.of("sstv_image")
    assert len(images) == 1 and images[0]["rows"] == 40 and images[0]["complete"] is False


# -- through the host --------------------------------------------------------------------


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
        pass


class BlockingAprs:
    name = "aprs"

    def env(self, slot, params, rx_node):
        return {}

    async def run(self, slot, emit):
        await asyncio.Event().wait()


class Sent:
    def __init__(self):
        self.frames = []

    async def __call__(self, msg):
        self.frames.append(msg)

    def outputs(self, kind):
        return [f for f in self.frames if f["type"] == "decoder_output" and f["kind"] == kind]


def _host(plugin, *extra, spool=None):
    units = FakeUnits()
    plugins = {p.name: p for p in (plugin, *extra)}
    host = DecoderHost(units, plugins, lambda slot: "rx", spool=spool or Spool(epoch="E"))
    return host, units


def test_stop_midpicture_finalizes_partial():
    async def go():
        async with FakeUnit() as unit:
            plugin = _plugin(unit)
            host, units = _host(plugin)
            sent = Sent()
            host.attach(sent)
            await host.start("sstv", 1, {}, "r1")
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(100)])
            await wait_for(lambda: len(sent.outputs("sstv_line")) == 100)
            await host.stop("sstv", 1, "r2")
            return sent, units

    sent, units = run(go())
    images = sent.outputs("sstv_image")
    assert len(images) == 1
    frame = images[0]
    assert frame["reliable"] is True and frame["seq"] >= 1
    data = frame["data"]
    assert data["complete"] is False and data["rows"] == 100
    rows, size = _png_rows(base64.b64decode(data["png"]))
    assert size == (W, H)
    assert rows[:100] == [_row(i) for i in range(100)]
    assert all(r == bytes(W * 3) for r in rows[100:])
    # finalize ran before the unit was stopped, the image before the "stopped" state.
    idx_image = sent.frames.index(frame)
    idx_stopped = next(
        i
        for i, f in enumerate(sent.frames)
        if f["type"] == "decoder_state" and f["state"] == "stopped"
    )
    assert idx_image < idx_stopped
    assert ("stop", "sstv", 1) in units.calls


def test_under_10_percent_discarded():
    async def go():
        async with FakeUnit() as unit:
            plugin = _plugin(unit)
            host, _ = _host(plugin)
            sent = Sent()
            host.attach(sent)
            await host.start("sstv", 1, {}, "r1")
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(20)])  # 20/256 < 10 %
            await wait_for(lambda: len(sent.outputs("sstv_line")) == 20)
            await host.stop("sstv", 1, "r2")
            return sent

    sent = run(go())
    assert sent.outputs("sstv_image") == []
    assert len(sent.outputs("sstv_start")) == 1


def test_finalize_timeout_does_not_block_stop(monkeypatch):
    class Hung:
        name = "sstv"

        def env(self, slot, params, rx_node):
            return {}

        async def run(self, slot, emit):
            await asyncio.Event().wait()

        async def finalize(self, slot, emit):
            await asyncio.Event().wait()

    monkeypatch.setattr("station_agent.decoders.host.FINALIZE_TIMEOUT_S", 0.05)

    async def go():
        host, units = _host(Hung())
        sent = Sent()
        host.attach(sent)
        await host.start("sstv", 1, {}, "r1")
        await asyncio.wait_for(host.stop("sstv", 1, "r2"), 2)
        return units, host

    units, host = run(go())
    assert ("stop", "sstv", 1) in units.calls and host.running() == []


def test_finalize_error_does_not_block_stop():
    class Broken(BlockingAprs):
        name = "sstv"

        async def finalize(self, slot, emit):
            raise RuntimeError("boom")

    async def go():
        host, units = _host(Broken())
        host.attach(Sent())
        await host.start("sstv", 1, {}, "r1")
        await host.stop("sstv", 1, "r2")
        return units, host

    units, host = run(go())
    assert ("stop", "sstv", 1) in units.calls and host.running() == []


def test_link_lost_stops_sstv_but_not_aprs():
    async def go():
        async with FakeUnit() as unit:
            plugin = _plugin(unit)
            host, units = _host(plugin, BlockingAprs())
            sent = Sent()
            host.attach(sent)
            await host.start("aprs", 1, {}, "r1")
            await host.start("sstv", 2, {}, "r2")
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(60)])
            await wait_for(lambda: len(sent.outputs("sstv_line")) == 60)

            host.detach()
            host.on_link_lost()
            await wait_for(lambda: all(r["decoder"] != "sstv" for r in host.running()))
            running = host.running()
            pending = list(host._spool.pending())
            before = len(sent.frames)

            sent2 = Sent()
            host.attach(sent2)
            await host.on_connected()
            return running, pending, before, sent, sent2, units

    running, pending, before, sent, sent2, units = run(go())
    assert running == [{"decoder": "aprs", "slot": 1, "state": "running"}]
    assert ("stop", "sstv", 2) in units.calls and ("stop", "aprs", 1) not in units.calls
    # The partial picture went to the spool, not to the dead link ...
    assert sent.outputs("sstv_image") == [] and len(sent.frames) == before
    spooled = [f for f in pending if f["kind"] == "sstv_image"]
    assert len(spooled) == 1 and spooled[0]["data"]["rows"] == 60
    # ... and is delivered after the reconnect, after a snapshot without sstv.
    assert sent2.frames[0]["type"] == "decoder_snapshot"
    assert all(d["decoder"] != "sstv" for d in sent2.frames[0]["decoders"])
    assert [f["data"]["rows"] for f in sent2.outputs("sstv_image")] == [60]


def test_link_lost_leaves_decoder_of_new_connection():
    async def go():
        async with FakeUnit() as unit:
            host, units = _host(_plugin(unit))
            host.attach(Sent())
            host.detach()
            host.on_link_lost()  # nothing ran on the old link
            host.attach(Sent())
            await host.start("sstv", 1, {}, "r1")
            await host.on_connected()
            await asyncio.sleep(0.05)
            return host.running(), units

    running, units = run(go())
    assert running == [{"decoder": "sstv", "slot": 1, "state": "running"}]
    assert ("stop", "sstv", 1) not in units.calls


def test_lines_not_spooled():
    async def go():
        async with FakeUnit() as unit:
            spool = Spool(epoch="E")
            host, _ = _host(_plugin(unit), spool=spool)
            await host.start("sstv", 1, {}, "r1")  # no link attached at all
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(30)], _end(False, 30))
            await wait_for(lambda: any(f["kind"] == "sstv_image" for f in spool.pending()))
            kinds = [f["kind"] for f in spool.pending()]
            await host.shutdown()
            return kinds

    kinds = run(go())
    assert kinds == ["sstv_start", "sstv_image"]


# -- feature advertisement ---------------------------------------------------------------


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def _inventory_features(plugins, sstv_tx=object()):
    host = DecoderHost(FakeUnits(), plugins, lambda slot: "rx")

    async def go():
        col = Collector()
        b = Broker(col, decoder_host=host, sstv_tx=sstv_tx)
        await b.emit_inventory()
        return [m for m in col.sent if m["type"] == "inventory"][0].get("features")

    return run(go())


def test_feature_advertised():
    features = _inventory_features({"aprs": BlockingAprs(), "sstv": SstvDecoderPlugin()})
    assert features == [decoders.FEATURE, decoders.SSTV_FEATURE]


def test_feature_not_advertised_without_plugin_or_tx():
    assert _inventory_features({"aprs": BlockingAprs()}) == [decoders.FEATURE]
    assert _inventory_features({"sstv": SstvDecoderPlugin()}, sstv_tx=None) == [decoders.FEATURE]


# -- end to end: encoder audio -> real rx_unit -> plugin -> host -> PNG ------------------


def test_e2e_real_unit_robot36():
    np = pytest.importorskip("numpy")
    import os
    import socket
    import threading

    from station_agent.sstv import rx_unit
    from station_agent.sstv.encoder import encode

    mode = MODES["robot36"]
    x = np.linspace(40, 215, mode.width)
    y = np.linspace(40, 215, mode.height)[:, None]
    shape = (mode.height, mode.width)
    pic = np.rint(
        np.stack(
            [
                np.broadcast_to(x, shape),
                np.broadcast_to(y, shape),
                np.broadcast_to(255 - x, shape),
            ],
            axis=2,
        )
    ).astype(np.uint8)
    pcm = encode(mode, pic.tobytes()) + b"\x00\x00" * 4000

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r, w = os.pipe()
    stdin, feeder = os.fdopen(r, "rb"), os.fdopen(w, "wb")
    ready = threading.Event()
    unit_ref = {}

    def started(unit):
        unit_ref["unit"] = unit
        ready.set()

    thread = threading.Thread(
        target=rx_unit.main,
        args=(["--port", str(port), "--rate", "8000"],),
        kwargs={"stdin": stdin, "started": started},
        daemon=True,
    )
    thread.start()
    assert ready.wait(5)

    async def connect(host, _port, **kw):
        return await asyncio.open_connection(host, port, **kw)

    async def go():
        plugin = SstvDecoderPlugin(connect=connect)
        host, _ = _host(plugin)
        sent = Sent()
        host.attach(sent)
        await host.start("sstv", 1, {}, "r1")
        await wait_for(lambda: unit_ref["unit"].has_client, 5)

        def feed():
            with feeder:  # EOF flushes the decoder: the unit sends the final "end"
                feeder.write(pcm)

        await asyncio.get_running_loop().run_in_executor(None, feed)
        await wait_for(lambda: sent.outputs("sstv_image"), 60)
        await host.stop("sstv", 1, "r2")
        return sent

    sent = run(go())
    thread.join(10)
    starts = sent.outputs("sstv_start")
    images = sent.outputs("sstv_image")
    assert len(starts) == 1 and len(images) == 1
    data = images[0]["data"]
    assert data["picture_id"] == starts[0]["data"]["picture_id"]
    assert data["mode"] == "robot36" and data["complete"] is True
    assert data["rows"] == mode.height
    assert len(sent.outputs("sstv_line")) == mode.height
    rows, size = _png_rows(base64.b64decode(data["png"]))
    assert size == (mode.width, mode.height)
    got = np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(pic.shape)
    assert np.mean(np.abs(got.astype(int) - pic.astype(int))) < 12


# -- round-1 review: size bound, stop races, replay edge cases -----------------------------


def test_noisy_pd180_frame_fits_ws_message_limit():
    import os

    mode = MODES["pd180"]
    rows = [os.urandom(mode.width * 3) for _ in range(mode.height)]

    async def go():
        async with FakeUnit() as unit:
            spool = Spool(epoch="E", max_bytes=8_000_000)
            host, _ = _host(_plugin(unit), spool=spool)
            await host.start("sstv", 1, {}, "r1")
            w = await unit.client()
            await send(
                w,
                _start(mode),
                *[
                    {"t": "row", "i": i, "rgb": base64.b64encode(r).decode("ascii")}
                    for i, r in enumerate(rows)
                ],
                _end(True, mode.height),
            )
            await wait_for(lambda: any(f["kind"] == "sstv_image" for f in spool.pending()), 30)
            frame = next(f for f in spool.pending() if f["kind"] == "sstv_image")
            await host.shutdown()
            return frame

    frame = run(go())
    assert len(json.dumps(frame)) < 1024 * 1024  # daphne's default websocket_max_message_size
    data = frame["data"]
    assert data["complete"] is True and data["depth"] < 8
    got, size = _png_rows(base64.b64decode(data["png"]))
    assert size == (mode.width, mode.height)
    keep = (0xFF << (8 - data["depth"])) & 0xFF
    assert got[3] == bytes(v & keep for v in rows[3])


def test_smooth_picture_keeps_full_depth():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(H)], _end(True, H))
            await wait_for(lambda: emits.of("sstv_image"))
            task.cancel()
            return emits.of("sstv_image")[0]

    assert run(go())["depth"] == 8


def _slow_encode(monkeypatch, seconds):
    """Make the PNG encode slow; the returned event is set once an encode has begun."""
    import threading
    import time

    real = sstv_mod._encode
    entered = threading.Event()

    def slow(*args):
        entered.set()
        time.sleep(seconds)
        return real(*args)

    monkeypatch.setattr(sstv_mod, "_encode", slow)
    return entered


def test_stop_while_finished_picture_encodes_keeps_it(monkeypatch):
    entered = _slow_encode(monkeypatch, 0.3)

    async def go():
        async with FakeUnit() as unit:
            host, _ = _host(_plugin(unit))
            sent = Sent()
            host.attach(sent)
            await host.start("sstv", 1, {}, "r1")
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(H)], _end(True, H))
            await wait_for(entered.is_set)  # reader is inside the encode now
            await host.stop("sstv", 1, "r2")
            return sent

    sent = run(go())
    images = sent.outputs("sstv_image")
    assert [i["data"]["complete"] for i in images] == [True]


def test_finalize_timeout_still_spools_partial(monkeypatch):
    _slow_encode(monkeypatch, 0.3)
    monkeypatch.setattr("station_agent.decoders.host.FINALIZE_TIMEOUT_S", 0.05)

    async def go():
        async with FakeUnit() as unit:
            spool = Spool(epoch="E")
            host, _ = _host(_plugin(unit), spool=spool)
            live = Sent()
            host.attach(live)
            await host.start("sstv", 1, {}, "r1")
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(100)])
            await wait_for(lambda: len(live.outputs("sstv_line")) == 100)
            await host.stop("sstv", 1, "r2")
            await wait_for(lambda: any(f["kind"] == "sstv_image" for f in spool.pending()))
            return [f for f in spool.pending() if f["kind"] == "sstv_image"]

    images = run(go())
    assert len(images) == 1 and images[0]["data"]["rows"] == 100


def test_lines_after_finalize_are_ignored():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(40)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 40)
            await plugin.finalize(1, emits)
            await send(w, _start(), _row_line(0))
            await asyncio.sleep(0.1)
            task.cancel()
            return emits

    emits = run(go())
    assert len(emits.of("sstv_start")) == 1 and len(emits.of("sstv_image")) == 1


def test_max_runtime_finalizes_partial():
    async def go():
        async with FakeUnit() as unit:
            expired = asyncio.Event()

            async def sleep(seconds):
                if seconds == 777:  # the max-runtime timer: fires when the test says so
                    await expired.wait()
                else:
                    await asyncio.sleep(seconds)

            host = DecoderHost(
                FakeUnits(), {"sstv": _plugin(unit)}, lambda slot: "rx", spool=Spool(), sleep=sleep
            )
            sent = Sent()
            host.attach(sent)
            await host.start("sstv", 1, {}, "r1", max_runtime_s=777)
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(50)])
            await wait_for(lambda: len(sent.outputs("sstv_line")) == 50)
            expired.set()
            await wait_for(lambda: sent.outputs("sstv_image"), 5)
            return sent

    sent = run(go())
    assert sent.outputs("sstv_image")[0]["data"]["rows"] == 50
    stopped = [f for f in sent.frames if f["type"] == "decoder_state" and f["state"] == "stopped"]
    assert stopped and stopped[0]["detail"] == "max_runtime"


def test_link_lost_catches_start_still_in_flight():
    """A start of the lost connection that has not finished yet is stopped too."""
    import threading

    gate = threading.Event()

    def slow_resolve(slot):
        gate.wait(5)
        return "rx"

    async def go():
        async with FakeUnit() as unit:
            units = FakeUnits()
            host = DecoderHost(units, {"sstv": _plugin(unit)}, slow_resolve, spool=Spool())
            host.attach(Sent())
            start = asyncio.create_task(host.start("sstv", 1, {}, "r1"))
            await wait_for(host._lock.locked)  # start holds the lock, resolving
            host.detach()
            host.on_link_lost()
            host.attach(Sent())  # reconnected before the old start finished
            gate.set()
            await start
            await host.on_connected()
            return host.running(), units

    running, units = run(go())
    assert running == [] and ("stop", "sstv", 1) in units.calls


def test_repeated_row_not_reemitted():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), _row_line(0), _row_line(0), _row_line(1))
            await wait_for(lambda: len(emits.of("sstv_line")) == 2)
            await asyncio.sleep(0.05)
            task.cancel()
            return emits

    assert [d["row"] for d in run(go()).of("sstv_line")] == [0, 1]


def test_replay_skipping_a_held_row_splits():
    """Our picture held row 5; the replayed one jumps from 4 to 6: different picture."""

    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit, sleep=lambda s: asyncio.sleep(0)), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(40)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 40)
            w.transport.abort()
            w2 = await unit.client()
            replay = [_row_line(i) for i in range(40) if i != 5]
            await send(w2, _start(), *replay, _row_line(40))
            await wait_for(lambda: len(emits.of("sstv_start")) == 2)
            await asyncio.sleep(0.05)
            task.cancel()
            return emits

    emits = run(go())
    first = emits.of("sstv_image")
    assert len(first) == 1 and first[0]["rows"] == 40
    new_id = emits.of("sstv_start")[1]["picture_id"]
    new_rows = [d["row"] for d in emits.of("sstv_line") if d["picture_id"] == new_id]
    assert 5 not in new_rows and set(new_rows) == set(range(41)) - {5}


def test_end_while_verifying_with_extra_held_rows_splits():
    """The replayed picture ends before reaching rows we hold: it was not ours."""

    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit, sleep=lambda s: asyncio.sleep(0)), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(60)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 60)
            w.transport.abort()
            w2 = await unit.client()
            await send(w2, _start(), *[_row_line(i) for i in range(30)], _end(False, 30))
            await wait_for(lambda: len(emits.of("sstv_image")) == 2)
            task.cancel()
            return emits

    emits = run(go())
    starts = emits.of("sstv_start")
    old, new = emits.of("sstv_image")
    assert old["picture_id"] == starts[0]["picture_id"] and old["rows"] == 60
    assert new["picture_id"] == starts[1]["picture_id"] and new["rows"] == 30


# -- round-2 review ------------------------------------------------------------------------


def test_start_during_finalize_opens_no_picture(monkeypatch):
    """Reader suspended in a picture's encode when finalize runs: no orphan sstv_start."""
    entered = _slow_encode(monkeypatch, 0.3)

    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            # The second start closes the first picture (slow encode) before opening one.
            await send(w, _start(), *[_row_line(i) for i in range(40)], _start())
            await wait_for(entered.is_set)  # reader is in the first picture's encode
            await plugin.finalize(1, emits)
            await wait_for(lambda: emits.of("sstv_image"))
            await asyncio.sleep(0.05)  # the reader resumes after the encode
            task.cancel()
            return plugin, emits

    plugin, emits = run(go())
    assert len(emits.of("sstv_start")) == 1 and len(emits.of("sstv_image")) == 1
    assert plugin._pictures == {}


def test_end_split_during_finalize_does_not_crash(monkeypatch):
    entered = _slow_encode(monkeypatch, 0.3)

    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit, sleep=lambda s: asyncio.sleep(0)), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(60)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 60)
            w.transport.abort()
            w2 = await unit.client()
            await send(w2, _start(), *[_row_line(i) for i in range(30)], _end(False, 30))
            await wait_for(entered.is_set)  # reader is encoding the old picture (split)
            await plugin.finalize(1, emits)
            await wait_for(lambda: emits.of("sstv_image"))
            await asyncio.sleep(0.05)  # the reader resumes after the encode
            done = task.done()
            task.cancel()
            return emits, done

    emits, crashed = run(go())
    assert not crashed  # the reader neither raised nor ended
    assert len(emits.of("sstv_start")) == 1 and len(emits.of("sstv_image")) == 1


def test_stop_then_start_same_slot_decodes_again():
    async def go():
        async with FakeUnit() as unit:
            host, _ = _host(_plugin(unit))
            sent = Sent()
            host.attach(sent)
            for n in (1, 2):
                await host.start("sstv", 1, {}, f"s{n}")
                w = await unit.client()
                await send(w, _start(), *[_row_line(i) for i in range(H)], _end(True, H))
                await wait_for(lambda: len(sent.outputs("sstv_image")) == n)
                await host.stop("sstv", 1, f"t{n}")
            return sent

    images = run(go()).outputs("sstv_image")
    assert [i["data"]["complete"] for i in images] == [True, True]


@pytest.mark.parametrize(("rows", "kept"), [(24, True), (23, False)])
def test_ten_percent_boundary(rows, kept):
    mode = MODES["robot36"]  # 240 rows: 24 is exactly 10 %

    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            line = {"t": "row", "rgb": base64.b64encode(bytes(mode.width * 3)).decode()}
            await send(
                w, _start(mode), *[{**line, "i": i} for i in range(rows)], _end(False, rows)
            )
            await send(w, _start(mode))  # marker
            await wait_for(lambda: len(emits.of("sstv_start")) == 2)
            task.cancel()
            return emits

    assert bool(run(go()).of("sstv_image")) is kept


def test_second_replay_reverifies():
    """Two reconnects: the second replay is checked afresh (verified set reset)."""

    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit, sleep=lambda s: asyncio.sleep(0)), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(40)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 40)
            w.transport.abort()
            w2 = await unit.client()
            await send(w2, _start(), *[_row_line(i) for i in range(20)])  # still verifying
            await asyncio.sleep(0.05)
            w2.transport.abort()
            w3 = await unit.client()
            # Different picture: row 0 already differs. A stale verified set would keep
            # rows 0..19 of the old picture as "verified".
            await send(w3, _start(), *[_row_line(i, salt=3) for i in range(45)])
            await wait_for(lambda: len(emits.of("sstv_start")) == 2)
            await wait_for(lambda: len(emits.of("sstv_line")) == 40 + 45)
            task.cancel()
            return emits

    emits = run(go())
    new_id = emits.of("sstv_start")[1]["picture_id"]
    new_lines = [d for d in emits.of("sstv_line") if d["picture_id"] == new_id]
    assert [d["row"] for d in new_lines] == list(range(45))
    assert new_lines[0]["rgb"] == _row_line(0, salt=3)["rgb"]


def test_end_split_below_10_percent_opens_no_ghost_picture():
    async def go():
        async with FakeUnit() as unit:
            plugin, emits = _plugin(unit, sleep=lambda s: asyncio.sleep(0)), Emits()
            task = asyncio.create_task(plugin.run(1, emits))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(60)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 60)
            w.transport.abort()
            w2 = await unit.client()
            await send(w2, _start(), *[_row_line(i) for i in range(10)], _end(False, 10))
            await wait_for(lambda: emits.of("sstv_image"))
            await asyncio.sleep(0.05)
            task.cancel()
            return emits

    emits = run(go())
    assert len(emits.of("sstv_start")) == 1
    assert [i["rows"] for i in emits.of("sstv_image")] == [60]


def test_link_lost_output_never_goes_live_on_new_connection(monkeypatch):
    """Fast reconnect while the link-lost finalize still encodes: spool only, in order."""
    entered = _slow_encode(monkeypatch, 0.3)

    async def go():
        async with FakeUnit() as unit:
            spool = Spool(epoch="E")
            host, _ = _host(_plugin(unit), spool=spool)
            old = Sent()
            host.attach(old)
            await host.start("sstv", 1, {}, "r1")
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(60)])
            await wait_for(lambda: len(old.outputs("sstv_line")) == 60)
            host.detach()
            host.on_link_lost()
            await wait_for(entered.is_set)
            live = Sent()
            host.attach(live)  # reconnected before the image is encoded
            await wait_for(lambda: any(f["kind"] == "sstv_image" for f in spool.pending()))
            await asyncio.sleep(0.05)
            before_snapshot = list(live.frames)
            await host.on_connected()
            return before_snapshot, live

    before_snapshot, live = run(go())
    assert before_snapshot == []
    assert live.frames[0]["type"] == "decoder_snapshot"
    images = live.outputs("sstv_image")
    assert len(images) == 1 and images[0]["data"]["rows"] == 60
    seqs = [f["seq"] for f in live.frames if f["type"] == "decoder_output"]
    assert seqs == sorted(seqs)


# -- round-3 review ------------------------------------------------------------------------


def test_link_lost_image_after_finalize_timeout_reaches_new_link(monkeypatch):
    """Finalize timed out, the new link's snapshot + resend already went out: the picture
    finished afterwards is sent live, not stranded in the spool."""
    entered = _slow_encode(monkeypatch, 0.3)
    monkeypatch.setattr("station_agent.decoders.host.FINALIZE_TIMEOUT_S", 0.05)

    async def go():
        async with FakeUnit() as unit:
            host, _ = _host(_plugin(unit))
            old = Sent()
            host.attach(old)
            await host.start("sstv", 1, {}, "r1")
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(60)])
            await wait_for(lambda: len(old.outputs("sstv_line")) == 60)
            host.detach()
            host.on_link_lost()
            await wait_for(entered.is_set)
            await wait_for(lambda: not host._link_lost_tasks)  # finalize timed out
            live = Sent()
            host.attach(live)
            await host.on_connected()
            resent = len(live.outputs("sstv_image"))
            await wait_for(lambda: live.outputs("sstv_image"))
            return resent, live

    resent, live = run(go())
    assert resent == 0
    assert live.frames[0]["type"] == "decoder_snapshot"
    assert [f["data"]["rows"] for f in live.outputs("sstv_image")] == [60]


def test_failed_link_loss_stop_reports_later_stop():
    from station_agent.decoders.units import DecoderUnitError

    class FlakyUnits(FakeUnits):
        def __init__(self):
            super().__init__()
            self.fail = True

        def stop(self, decoder, slot):
            super().stop(decoder, slot)
            if self.fail:
                self.fail = False
                raise DecoderUnitError("busy")

    async def go():
        units = FlakyUnits()
        host = DecoderHost(units, {"sstv": BlockingSstv()}, lambda slot: "rx", spool=Spool())
        host.attach(Sent())
        await host.start("sstv", 1, {}, "r1")
        host.detach()
        host.on_link_lost()
        await wait_for(lambda: not host._link_lost_tasks)
        live = Sent()
        host.attach(live)
        await host.on_connected()
        await host.stop("sstv", 1, "r2")
        return live, host

    live, host = run(go())
    snapshot = live.frames[0]
    assert snapshot["decoders"] == [{"decoder": "sstv", "slot": 1, "state": "error"}]
    states = [f for f in live.frames if f["type"] == "decoder_state"]
    assert [s["state"] for s in states] == ["stopped"] and host.running() == []


class BlockingSstv(BlockingAprs):
    name = "sstv"
    stop_on_link_loss = True


def test_on_connected_right_after_link_loss_waits_for_the_stop():
    async def go():
        async with FakeUnit() as unit:
            host, _ = _host(_plugin(unit))
            host.attach(Sent())
            await host.start("sstv", 1, {}, "r1")
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(40)])
            await wait_for(lambda: host._plugins["sstv"]._pictures.get(1) is not None)
            await asyncio.sleep(0.05)
            host.detach()
            host.on_link_lost()
            live = Sent()
            host.attach(live)
            await host.on_connected()  # no wait in between
            return live

    live = run(go())
    assert live.frames[0] == {"v": 1, "type": "decoder_snapshot", "decoders": []}
    assert [f["data"]["rows"] for f in live.outputs("sstv_image")] == [40]


def test_link_lost_unreliable_output_never_live():
    gate = asyncio.Event

    class LateEmitter(BlockingSstv):
        def __init__(self):
            self.release = None

        async def finalize(self, slot, emit):
            await self.release.wait()
            await emit("x_line", False, {"n": 1})
            await emit("x_image", True, {"n": 2})

    async def go():
        plugin = LateEmitter()
        plugin.release = gate()
        host = DecoderHost(FakeUnits(), {"sstv": plugin}, lambda slot: "rx", spool=Spool())
        host.attach(Sent())
        await host.start("sstv", 1, {}, "r1")
        host.detach()
        host.on_link_lost()
        await asyncio.sleep(0.01)
        live = Sent()
        host.attach(live)  # new link before the late output
        plugin.release.set()
        await wait_for(lambda: not host._link_lost_tasks)
        before = list(live.frames)
        await host.on_connected()
        return before, live

    before, live = run(go())
    assert before == []  # neither the line nor the image went out before the snapshot
    kinds = [f.get("kind") for f in live.frames if f["type"] == "decoder_output"]
    assert kinds == ["x_image"]


def test_finalize_runs_before_the_reader_is_cancelled():
    order = []

    class Recording(BlockingAprs):
        name = "sstv"

        async def run(self, slot, emit):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("cancelled")
                raise

        async def finalize(self, slot, emit):
            order.append("finalize")

    async def go():
        host, _ = _host(Recording())
        host.attach(Sent())
        await host.start("sstv", 1, {}, "r1")
        await asyncio.sleep(0.01)
        await host.stop("sstv", 1, "r2")

    run(go())
    assert order == ["finalize", "cancelled"]


def test_finalize_while_split_opens_new_picture():
    """finalize lands while the reader is suspended in the new picture's sstv_start emit:
    the reader must not go on with the picture finalize already took."""

    async def go():
        async with FakeUnit() as unit:
            plugin = _plugin(unit, sleep=lambda s: asyncio.sleep(0))
            emits, blocked, release = Emits(), asyncio.Event(), asyncio.Event()

            async def emit(kind, reliable, data):
                await emits(kind, reliable, data)
                if kind == "sstv_start" and len(emits.of("sstv_start")) == 2:
                    blocked.set()
                    await release.wait()

            task = asyncio.create_task(plugin.run(1, emit))
            w = await unit.client()
            await send(w, _start(), *[_row_line(i) for i in range(40)])
            await wait_for(lambda: len(emits.of("sstv_line")) == 40)
            w.transport.abort()
            w2 = await unit.client()
            # rows 0..29 match, row 30 differs -> split; new picture gets rows 0..29
            await send(w2, _start(), *[_row_line(i) for i in range(30)], _row_line(30, salt=9))
            await wait_for(blocked.is_set)
            lines_before = len(emits.of("sstv_line"))
            await plugin.finalize(1, emits)
            release.set()
            await asyncio.sleep(0.05)
            task.cancel()
            return plugin, emits, lines_before

    plugin, emits, lines_before = run(go())
    assert plugin._pictures == {}
    # No line for row 30 (nor the replayed rows) after finalize took the new picture.
    assert len(emits.of("sstv_line")) == lines_before
    assert [i["rows"] for i in emits.of("sstv_image")] == [40, 30]
