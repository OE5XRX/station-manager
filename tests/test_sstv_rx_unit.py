"""SSTV decoder unit entry point (station_agent/sstv/rx_unit.py)."""

import base64
import io
import json
import os
import socket
import threading
import time

import numpy as np
import pytest

from station_agent.sstv import rx_unit
from station_agent.sstv.encoder import encode
from station_agent.sstv.modes import MODES

SR = 8000


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _picture(m):
    """Smooth gradients (8 kHz SSTV cannot carry per-pixel noise)."""
    x = np.linspace(40, 215, m.width)
    y = np.linspace(40, 215, m.height)[:, None]
    pic = np.stack(
        [
            np.broadcast_to(x, (m.height, m.width)),
            np.broadcast_to(y, (m.height, m.width)),
            np.broadcast_to(255 - x, (m.height, m.width)),
        ],
        axis=2,
    )
    return np.rint(pic).astype(np.uint8)


def _pcm(m, pic, tail_s=0.5):
    return encode(m, pic.tobytes()) + b"\x00\x00" * int(tail_s * SR)


class _Unit:
    """Runs ``rx_unit.main`` in a thread with a pipe as stdin."""

    def __init__(self, port, stdin=None, rate=SR):
        self.port = port
        if stdin is None:
            r, w = os.pipe()
            self.stdin = os.fdopen(r, "rb")
            self.feeder = os.fdopen(w, "wb")
        else:
            self.stdin, self.feeder = stdin, None
        self.rc = None
        self.unit = None
        self.ready = threading.Event()
        argv = ["--port", str(port), "--rate", str(rate)]

        def started(unit):
            self.unit = unit
            self.ready.set()

        def run():
            self.rc = rx_unit.main(argv, stdin=self.stdin, started=started)
            self.ready.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait_client(self, timeout=5.0):
        # The unit registers a client asynchronously after accept().
        deadline = time.monotonic() + timeout
        while not self.unit.has_client:
            assert time.monotonic() < deadline, "client never registered"
            time.sleep(0.01)

    def connect(self, timeout=5.0):
        assert self.ready.wait(timeout)
        deadline = time.monotonic() + timeout
        while True:
            try:
                return socket.create_connection(("127.0.0.1", self.port), timeout=10)
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)

    def write(self, data, chunk=SR * 2):
        for i in range(0, len(data), chunk):
            self.feeder.write(data[i : i + chunk])
        self.feeder.flush()

    def close_stdin(self):
        self.feeder.close()

    def join(self, timeout=30):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "rx_unit did not exit"
        return self.rc


def _read_lines(sock):
    buf = b""
    while chunk := sock.recv(65536):
        buf += chunk
    assert buf.endswith(b"\n")
    return [json.loads(line) for line in buf.decode("utf-8").splitlines()]


def test_full_picture_json_sequence():
    m = MODES["robot36"]
    pic = _picture(m)
    unit = _Unit(_free_port())
    sock = unit.connect()
    unit.wait_client()
    unit.write(_pcm(m, pic))
    unit.close_stdin()
    lines = _read_lines(sock)
    assert unit.join() == 0

    assert lines[0] == {"t": "start", "mode": "robot36", "width": 320, "height": 240}
    rows = lines[1:-1]
    assert [r["t"] for r in rows] == ["row"] * 240
    assert [r["i"] for r in rows] == list(range(240))
    got = np.frombuffer(b"".join(base64.b64decode(r["rgb"]) for r in rows), dtype=np.uint8)
    got = got.reshape(240, 320, 3).astype(int)
    assert np.mean(np.abs(got - pic.astype(int))) < 8
    assert lines[-1] == {"t": "end", "complete": True, "rows": 240}


def test_eof_mid_picture_ends_incomplete():
    m = MODES["robot36"]
    pcm = encode(m, _picture(m).tobytes())
    unit = _Unit(_free_port())
    sock = unit.connect()
    unit.wait_client()
    unit.write(pcm[: len(pcm) // 2])
    unit.close_stdin()
    lines = _read_lines(sock)
    assert unit.join() == 0
    assert lines[0]["t"] == "start"
    assert lines[-1]["t"] == "end" and lines[-1]["complete"] is False
    assert lines[-1]["rows"] == len(lines) - 2


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--port", "8200"],
        ["--port", "x", "--rate", "8000"],
        ["--port", "0", "--rate", "8000"],
        ["--port", "70000", "--rate", "8000"],
        ["--port", "8200", "--rate", "100"],
        ["--port", "8200", "--rate", "8000", "--bogus"],
    ],
)
def test_bad_args_exit_2(argv):
    assert rx_unit.main(argv, stdin=io.BytesIO(b"")) == 2


def test_no_client_does_not_block():
    m = MODES["martin2"]
    unit = _Unit(_free_port(), stdin=io.BytesIO(_pcm(m, _picture(m))))
    assert unit.join(timeout=30) == 0


def test_empty_stdin_exits_0():
    unit = _Unit(_free_port(), stdin=io.BytesIO(b""))
    assert unit.join() == 0


def test_newest_client_replaces_older_and_gets_the_picture_replayed():
    m = MODES["martin2"]
    pcm = _pcm(m, _picture(m))
    unit = _Unit(_free_port())
    first = unit.connect()
    unit.wait_client()
    cut = int((0.91 + 10.0) * SR) * 2  # VIS + 10 s of picture
    unit.write(pcm[:cut])
    # Wait until the first client has seen the start, then reconnect.
    first.settimeout(10)
    seen = b""
    while b'"t":"start"' not in seen and b'"t": "start"' not in seen:
        seen += first.recv(65536)
    second = unit.connect()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:  # the first one is closed by the unit
        try:
            if first.recv(65536) == b"":
                break
        except TimeoutError:
            pass
    unit.write(pcm[cut:])
    unit.close_stdin()
    lines = _read_lines(second)
    assert unit.join() == 0
    assert lines[0] == {"t": "start", "mode": "martin2", "width": 320, "height": 256}
    assert lines[-1] == {"t": "end", "complete": True, "rows": 256}
    # start + every row exactly once: the rows sent to the first client are replayed.
    assert [r["i"] for r in lines[1:-1]] == list(range(256))


def test_listens_on_localhost_only():
    port = _free_port()
    unit = _Unit(port)
    unit.connect().close()
    assert unit.unit.addresses
    assert all(addr == ("127.0.0.1", port) for addr in unit.unit.addresses)
    unit.close_stdin()
    assert unit.join() == 0


def test_port_base():
    assert rx_unit.SSTV_PORT_BASE == 8200


def test_module_entry_point_exit_codes():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "-m", "station_agent.sstv.rx_unit"]
    bad = subprocess.run(cmd + ["--rate", "8000"], cwd=root, capture_output=True, timeout=30)
    assert bad.returncode == 2
    port = str(_free_port())
    ok = subprocess.run(
        cmd + ["--port", port, "--rate", "8000"],
        cwd=root,
        input=b"",
        capture_output=True,
        timeout=30,
    )
    assert ok.returncode == 0, ok.stderr


def test_client_that_stops_reading_is_dropped():
    # The kernel's localhost socket buffers absorb a whole picture, so drive the
    # policy directly: a client whose write buffer is past the limit is dropped
    # instead of growing the unit's memory (decoding never waits for a client).
    class _Transport:
        def __init__(self, size):
            self.size = size

        def get_write_buffer_size(self):
            return self.size

    class _Writer:
        def __init__(self, size):
            self.transport = _Transport(size)
            self.closed = False
            self.written = []

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

        def write(self, data):
            self.written.append(data)

    unit = rx_unit.RxUnit(_free_port(), SR, io.BytesIO(b""))
    ok = _Writer(rx_unit.MAX_CLIENT_BUFFER)
    unit._client = ok
    unit._send([b"a\n"])
    assert ok.written == [b"a\n"] and unit.has_client

    stalled = _Writer(rx_unit.MAX_CLIENT_BUFFER + 1)
    unit._client = stalled
    unit._send([b"b\n"])
    assert stalled.closed and stalled.written == [] and not unit.has_client


def test_exit_with_a_connected_client_that_never_reads():
    m = MODES["robot36"]
    unit = _Unit(_free_port())
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    assert unit.ready.wait(5)
    sock.connect(("127.0.0.1", unit.port))
    unit.wait_client()
    unit.write(_pcm(m, _picture(m)))
    unit.close_stdin()
    assert unit.join() == 0
    sock.close()


def test_drain_timeout_on_exit(monkeypatch):
    import asyncio

    monkeypatch.setattr(rx_unit, "DRAIN_TIMEOUT_S", 0.1)

    class _HangingWriter:
        closed = False

        async def drain(self):
            await asyncio.sleep(60)

        def close(self):
            self.closed = True

    unit = rx_unit.RxUnit(_free_port(), SR, io.BytesIO(b""))
    writer = _HangingWriter()
    unit._client = writer
    t0 = time.monotonic()
    asyncio.run(unit._close_client())
    assert time.monotonic() - t0 < 5
    assert writer.closed and not unit.has_client
