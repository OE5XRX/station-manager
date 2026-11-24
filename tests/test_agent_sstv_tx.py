"""Station-side SSTV TX session (SSTV spec §5.4, plan Task 6).

Real broker + pty fake firmware; fake HTTP download, fake player process. Every unkey path
asserts the PTT ends low and the session reports aborted/error.
"""

import asyncio
import hashlib

import pytest

from station_agent import sstv_tx
from station_agent.broker import Broker
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

FM_CAPS = [
    {
        "name": "frequency",
        "kind": "setting",
        "type": "float",
        "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}],
    },
    {"name": "ptt", "kind": "action", "type": "bool"},
]
FM = {"schema": 1, "module": "fm", "identity": {"type": "fm_transceiver"}, "capabilities": FM_CAPS}
SERVER = "https://sm.example"
AUDIO = bytes(range(256)) * 64  # 16 KiB "PCM"
SHA = hashlib.sha256(AUDIO).hexdigest()
PATH = "/api/v1/stations/sstv/tx/t1/audio/"


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)

    def states(self, tx_id="t1"):
        return [
            (m["state"], m["detail"])
            for m in self.sent
            if m["type"] == "sstv_tx_state" and m["tx_id"] == tx_id
        ]

    def result(self, rid):
        return next(m for m in self.sent if m["type"] == "result" and m["request_id"] == rid)


class FakeResponse:
    def __init__(self, body, status=200, headers=None, chunk=4096):
        self._body, self.status_code, self._chunk = body, status, chunk
        self.headers = headers if headers is not None else {"Content-Length": str(len(body))}
        self.closed = False

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i : i + self._chunk]

    def close(self):
        self.closed = True


class FakeHttp:
    def __init__(self, body=AUDIO, **kw):
        self.body, self.kw, self.paths, self.responses = body, kw, [], []
        self.block = None  # threading.Event: the GET blocks until it is set

    def get(self, path):
        self.paths.append(path)
        if self.block is not None:
            self.block.wait(5)
        resp = FakeResponse(self.body, **self.kw)
        self.responses.append(resp)
        return resp


class FakeProc:
    def __init__(self, mode, run_s):
        self.mode, self.run_s = mode, run_s
        self.returncode = None
        self.killed = False
        self._done = asyncio.Event()
        if mode != "hang":
            asyncio.get_running_loop().call_later(run_s, self._exit, 0 if mode == "ok" else 1)

    def _exit(self, rc):
        if self.returncode is None:
            self.returncode = rc
            self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self._exit(-9)


class FakePlayer:
    def __init__(self, mode="ok", run_s=0.1):
        self.mode, self.run_s = mode, run_s
        self.argvs, self.procs = [], []

    async def spawn(self, argv):
        self.argvs.append(argv)
        proc = FakeProc(self.mode, self.run_s)
        self.procs.append(proc)
        return proc


@pytest.fixture
def fw():
    f = FakeFirmware({"fm": FM})
    f.start()
    yield f
    f.stop()


def _manager(tmp_path, http=None, player=None, **kw):
    kw.setdefault("hard_stop_margin_s", 5.0)
    kw.setdefault("progress_interval_s", 0.05)
    return sstv_tx.SstvTxManager(
        http_get=(http or FakeHttp()).get,
        server_url=SERVER,
        ceiling_dbfs=lambda: -6.0,
        tx_node=lambda slot: f"alsa_output.slot{slot}",
        runtime_dir=str(tmp_path / "rt"),
        spawn=(player or FakePlayer()).spawn,
        **kw,
    )


def _broker(fw, mgr, dead_man_timeout=1.5):
    col = Collector()
    b = Broker(
        col,
        transport_factory=lambda p: SlotControl(p, timeout=2.0),
        dead_man_timeout=dead_man_timeout,
        now=lambda: 1.0,
        sstv_tx=mgr,
    )
    b.set_inventory(
        [
            {
                "slot": 1,
                "control": fw.control_path,
                "modules": [{"id": "fm", "identity": FM["identity"], "capabilities": FM_CAPS}],
            }
        ]
    )
    return b, col


def _start(rid="r1", tx_id="t1", **over):
    msg = {
        "v": 1,
        "type": "sstv_tx_start",
        "request_id": rid,
        "tx_id": tx_id,
        "slot": 1,
        "module": "fm",
        "url": SERVER + PATH,
        "sha256": SHA,
        "size": len(AUDIO),
        "duration_s": 0.1,
    }
    msg.update(over)
    return msg


def _ptt(fw, value):
    return sum(1 for line in fw.received if line == f"module fm do ptt {value}".encode())


async def _until_keyed(col, tx_id="t1", timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while ("keyed", None) not in col.states(tx_id):
        assert loop.time() < end, f"never keyed: {col.states(tx_id)}"
        await asyncio.sleep(0.01)


def _assert_low(fw):
    assert fw.state["fm"].get("ptt") == "false"
    assert _ptt(fw, "false") >= 1


def test_happy_path(fw, tmp_path):
    http, player = FakeHttp(), FakePlayer("ok", 0.1)
    mgr = _manager(tmp_path, http, player)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start())
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.result("r1")["ok"] is True
    states = [s for s, _ in col.states()]
    assert states[0] == "downloading" and states[-1] == "done" and "keyed" in states
    assert states.index("keyed") < len(states) - 1
    assert _ptt(fw, "true") == 1 and _ptt(fw, "false") == 1
    assert fw.state["fm"]["ptt"] == "false"
    assert http.paths == [PATH]
    assert http.responses[0].closed
    argv = player.argvs[0]
    assert "target-object=alsa_output.slot1" in argv
    loc = next(a for a in argv if a.startswith("location="))
    assert loc.startswith("location=" + str(tmp_path / "rt"))
    assert list((tmp_path / "rt").iterdir()) == []  # temp file deleted
    done = [m for m in col.sent if m["type"] == "sstv_tx_state"][-1]
    assert done["progress"] == 1.0
    assert not b.slot_busy(1)


def test_checksum_mismatch_never_keys(fw, tmp_path):
    mgr = _manager(tmp_path, FakeHttp(body=b"x" * len(AUDIO)))
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start())
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.states()[-1] == ("error", "checksum_mismatch")
    assert _ptt(fw, "true") == 0
    assert list((tmp_path / "rt").iterdir()) == []


@pytest.mark.parametrize(
    "body,headers,detail",
    [
        (AUDIO + b"extra", {}, "size_mismatch"),  # more bytes than declared
        (AUDIO[:-2], {}, "size_mismatch"),  # fewer bytes than declared
        (AUDIO, {"Content-Length": str(len(AUDIO) + 1)}, "size_mismatch"),
    ],
)
def test_oversize_never_keys(fw, tmp_path, body, headers, detail):
    mgr = _manager(tmp_path, FakeHttp(body=body, headers=headers))
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start())
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.states()[-1] == ("error", detail)
    assert _ptt(fw, "true") == 0


def test_oversize_frame_refused(fw, tmp_path):
    http = FakeHttp()
    mgr = _manager(tmp_path, http)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(size=sstv_tx.MAX_AUDIO_BYTES + 1))
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.result("r1")["error"]["code"] == "bad_request"
    assert http.paths == [] and _ptt(fw, "true") == 0


def test_download_http_error_never_keys(fw, tmp_path):
    mgr = _manager(tmp_path, FakeHttp(status=403))
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start())
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.states()[-1] == ("error", "http_403")
    assert _ptt(fw, "true") == 0


def test_hard_stop_at_duration_plus_5(fw, tmp_path):
    assert sstv_tx.HARD_STOP_MARGIN_S == 5.0
    player = FakePlayer("hang")
    mgr = _manager(tmp_path, player=player, hard_stop_margin_s=0.2)
    b, col = _broker(fw, mgr)

    async def scenario():
        loop = asyncio.get_running_loop()
        await b.handle(_start(duration_s=0.1))
        await _until_keyed(col)
        t0 = loop.time()
        await mgr.wait_idle()
        return loop.time() - t0

    elapsed = asyncio.run(scenario())
    assert col.states()[-1] == ("aborted", "hard_stop")
    assert 0.2 <= elapsed < 1.5
    assert player.procs[0].killed
    _assert_low(fw)


def test_default_hard_stop_deadline_is_duration_plus_5(tmp_path):
    mgr = _manager(tmp_path, hard_stop_margin_s=sstv_tx.HARD_STOP_MARGIN_S)
    assert mgr.hard_stop_after(114.0) == 119.0


def test_tx_stop_frame(fw, tmp_path):
    player = FakePlayer("hang")
    mgr = _manager(tmp_path, player=player)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(duration_s=30))
        await _until_keyed(col)
        await b.handle({"v": 1, "type": "sstv_tx_stop", "tx_id": "other"})
        await asyncio.sleep(0.05)
        assert fw.state["fm"]["ptt"] == "true"  # a foreign tx_id stops nothing
        await b.handle({"v": 1, "type": "sstv_tx_stop", "tx_id": "t1"})
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.states()[-1] == ("aborted", "stopped")
    assert player.procs[0].killed
    _assert_low(fw)


def test_tx_stop_during_download_never_keys(fw, tmp_path):
    import threading

    http = FakeHttp()
    http.block = threading.Event()
    mgr = _manager(tmp_path, http)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start())
        await asyncio.sleep(0.05)
        await b.handle({"v": 1, "type": "sstv_tx_stop", "tx_id": "t1"})
        await mgr.wait_idle()
        http.block.set()
        await asyncio.sleep(0.1)  # the download thread finishes late; still no key

    asyncio.run(scenario())
    assert col.states()[-1] == ("aborted", "stopped")
    assert _ptt(fw, "true") == 0
    assert list((tmp_path / "rt").iterdir()) == []


def test_ws_disconnect_aborts(fw, tmp_path):
    player = FakePlayer("hang")
    mgr = _manager(tmp_path, player=player)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(duration_s=30))
        await _until_keyed(col)
        await b.on_disconnect()
        # on_disconnect returns only once the transmitter is low.
        assert fw.state["fm"]["ptt"] == "false"
        assert not b.slot_busy(1)

    asyncio.run(scenario())
    assert col.states()[-1] == ("aborted", "ws_disconnect")
    assert player.procs[0].killed
    _assert_low(fw)


def test_player_crash(fw, tmp_path):
    player = FakePlayer("crash", 0.05)
    mgr = _manager(tmp_path, player=player)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(duration_s=30))
        await mgr.wait_idle()

    asyncio.run(scenario())
    state, detail = col.states()[-1]
    assert state == "error" and detail.startswith("player_failed")
    _assert_low(fw)


def test_player_spawn_failure(fw, tmp_path):
    async def boom(argv):
        raise FileNotFoundError("gst-launch-1.0")

    mgr = _manager(tmp_path)
    mgr._spawn = boom
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(duration_s=30))
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.states()[-1] == ("error", "player_failed")
    _assert_low(fw)


def test_agent_shutdown(fw, tmp_path):
    player = FakePlayer("hang")
    mgr = _manager(tmp_path, player=player)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(duration_s=30))
        await _until_keyed(col)
        await mgr.shutdown()
        assert fw.state["fm"]["ptt"] == "false"
        # After shutdown no new session starts.
        await b.handle(_start(rid="r2", tx_id="t2"))

    asyncio.run(scenario())
    assert col.states()[-1] == ("aborted", "shutdown")
    assert col.result("r2")["ok"] is False
    assert _ptt(fw, "true") == 1
    _assert_low(fw)


def test_no_dead_man_during_session(fw, tmp_path):
    player = FakePlayer("ok", 0.5)
    mgr = _manager(tmp_path, player=player)
    b, col = _broker(fw, mgr, dead_man_timeout=0.1)

    async def scenario():
        await b.handle(_start(duration_s=0.5))
        await _until_keyed(col)
        await asyncio.sleep(0.3)  # 3× the dead-man timeout without any keepalive
        assert fw.state["fm"]["ptt"] == "true"
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert not [m for m in col.sent if m.get("event") == "ptt_auto_unkey"]
    assert col.states()[-1][0] == "done"
    assert _ptt(fw, "true") == 1 and _ptt(fw, "false") == 1


def test_second_start_refused_sstv_busy(fw, tmp_path):
    mgr = _manager(tmp_path, player=FakePlayer("hang"))
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(duration_s=30))
        await b.handle(_start(rid="r2", tx_id="t2"))
        await b.handle({"v": 1, "type": "sstv_tx_stop", "tx_id": "t1"})
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.result("r1")["ok"] is True
    assert col.result("r2")["error"]["code"] == "sstv_busy"
    assert col.states("t2") == []
    assert _ptt(fw, "true") <= 1


def test_refused_while_voice_ptt_keyed(fw, tmp_path):
    http = FakeHttp()
    mgr = _manager(tmp_path, http)
    b, col = _broker(fw, mgr, dead_man_timeout=5.0)

    async def scenario():
        await b.handle(
            {
                "v": 1,
                "type": "command",
                "request_id": "k1",
                "slot": 1,
                "module": "fm",
                "capability": "ptt",
                "op": "do",
                "value": True,
            }
        )
        await b.handle(_start())
        await b.on_disconnect()

    asyncio.run(scenario())
    assert col.result("r1")["error"]["code"] == "slot_busy"
    assert http.paths == []


def test_voice_ptt_and_settings_refused_during_session(fw, tmp_path):
    mgr = _manager(tmp_path, player=FakePlayer("hang"))
    b, col = _broker(fw, mgr)

    def cmd(rid, cap, op, value):
        return {
            "v": 1,
            "type": "command",
            "request_id": rid,
            "slot": 1,
            "module": "fm",
            "capability": cap,
            "op": op,
            "value": value,
        }

    async def scenario():
        await b.handle(_start(duration_s=30))
        await _until_keyed(col)
        await b.handle(cmd("k1", "ptt", "do", True))
        await b.handle(cmd("s1", "frequency", "set", 145.5))
        assert fw.state["fm"]["ptt"] == "true"
        # A voice unkey always goes through — and ends the SSTV session.
        await b.handle(cmd("k2", "ptt", "do", False))
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.result("k1")["error"]["code"] == "sstv_busy"
    assert col.result("s1")["error"]["code"] == "sstv_busy"
    assert col.result("k2")["ok"] is True
    assert col.states()[-1] == ("aborted", "ptt_off")
    assert not [m for m in col.sent if m.get("event") == "ptt_auto_unkey"]
    _assert_low(fw)


def test_reconnect_never_rekeys(fw, tmp_path):
    player = FakePlayer("hang")
    mgr = _manager(tmp_path, player=player)
    b1, col1 = _broker(fw, mgr)

    async def scenario():
        await b1.handle(_start(duration_s=30))
        await _until_keyed(col1)
        await b1.on_disconnect()
        b2, col2 = _broker(fw, mgr)  # the next connection's broker, same process
        await b2.handle(_start(rid="r9"))  # the server replays the old start
        await mgr.wait_idle()
        return col2

    col2 = asyncio.run(scenario())
    assert col2.result("r9")["ok"] is False
    assert col2.result("r9")["error"]["code"] == "bad_request"
    assert col2.states() == []
    assert _ptt(fw, "true") == 1
    _assert_low(fw)


@pytest.mark.parametrize(
    "over",
    [
        {"tx_id": ""},
        {"tx_id": 5},
        {"slot": True},
        {"module": 3},
        {"sha256": "abc"},
        {"sha256": SHA.upper()},
        {"size": 0},
        {"size": True},
        {"duration_s": 0},
        {"duration_s": float("nan")},
        {"duration_s": 10_000},
        {"url": "https://evil.example" + PATH},
        {"url": "//evil.example" + PATH},
        {"url": SERVER + "@evil.example/x"},
        {"url": 7},
    ],
)
def test_malformed_start_refused(fw, tmp_path, over):
    http = FakeHttp()
    mgr = _manager(tmp_path, http)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(**over))
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.result("r1")["error"]["code"] == "bad_request"
    assert http.paths == [] and _ptt(fw, "true") == 0


def test_relative_url_accepted(fw, tmp_path):
    http = FakeHttp()
    mgr = _manager(tmp_path, http)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(url=PATH))
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert http.paths == [PATH]
    assert col.states()[-1][0] == "done"


def test_unknown_module_errors_without_keying(fw, tmp_path):
    mgr = _manager(tmp_path)
    b, col = _broker(fw, mgr)

    async def scenario():
        await b.handle(_start(module="nope"))
        await mgr.wait_idle()

    asyncio.run(scenario())
    assert col.states()[-1][0] == "error"
    assert _ptt(fw, "true") == 0


def test_without_manager_unsupported(fw):
    col = Collector()
    b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0))
    asyncio.run(b.handle(_start()))
    assert col.result("r1")["error"]["code"] == "unsupported"


def test_play_argv_gain():
    gain = sstv_tx.tx_gain(-6.0)
    assert gain == pytest.approx(10 ** (-6 / 20) / 0.9)
    assert gain == pytest.approx(0.5569, abs=1e-4)
    # The ceiling is re-clamped: a hostile value never gets louder than the max ceiling.
    assert sstv_tx.tx_gain(0.0) == pytest.approx(10 ** (-3 / 20) / 0.9)
    argv = sstv_tx.build_play_argv("/run/x/a.pcm", "alsa_output.slot1", gain)
    assert argv[0] == "gst-launch-1.0"
    joined = " ".join(argv)
    assert (
        "filesrc location=/run/x/a.pcm ! rawaudioparse format=pcm pcm-format=s16le "
        "sample-rate=8000 num-channels=1 ! audioconvert ! audioresample ! "
        "volume volume=0.556" in joined
    )
    assert joined.endswith("! pipewiresink target-object=alsa_output.slot1")
