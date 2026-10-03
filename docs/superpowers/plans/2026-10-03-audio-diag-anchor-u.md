# Anchor U (server-originated headless reference) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make anchor **U** of the TX audio-path diagnostics harness functional end-to-end on a real station — a server-originated Opus reference fed browserlessly through the op.mic path into an engine-managed diagnostic TX bridge that measures C (`output_MONO`) and D (ALSA edge), needing no PTT session.

**Architecture:** On `diag_command` with `anchor="U"` the agent engine spins up a **non-blocking** diagnostic TX bridge (`udpsrc→opusdec→resample→tee→[pipewiresink output_MONO][fdsink measure]`, i.e. the already-present `build_measured_tx_argv`), returns control immediately, and routes the server-streamed reference media frames into that bridge via the normal `on_media_frame` path. A background task reads the fdsink PCM tap, computes C + projects D = C + sink-volume, tears the bridge down, and emits `diag_result` over the WS. The server orchestrator streams the committed Opus fixture through the channel layer using a dedicated diagnostic stream-ref; the REST endpoint is un-gated for U and returns 409 when the station is busy (PTT/mic up or another diagnostic running).

**Tech Stack:** Python 3.14, Django 6.0 / DRF 3.17 / Django Channels, GStreamer `gst-launch-1.0`, PipeWire/WirePlumber, Opus, asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-10-02-audio-path-diagnostics-design.md`

## Global Constraints

- **RF SAFETY (non-negotiable):** purely digital. The diagnostic must NOT key the SA818 and must produce no RF. Writing samples to the PipeWire sink → ALSA → USB does not key the transmitter (keying is a separate control-plane PTT action). The engine MUST refuse a U run while a TX bridge is active (`self._tx is not None`). Measurement ends at the digital ALSA/UAC2 edge (D).
- **Non-blocking agent:** `on_diag_command` for U must not block the WS message loop, because the reference media frames arrive as subsequent WS messages and are processed by the same serial `async for` loop. It starts the bridge + a background measurement task and returns `None`.
- **Fail-closed hygiene (mirror existing code):** a failed bridge start/measure must release the UDP port, close pipe fds, and terminate the gst process — never leak on repeated failures on a long-running agent.
- **Django template comment rule:** never multi-line `{# … #}`; use `{% comment %}…{% endcomment %}` (CI guard active). (No templates touched here, but the rule stands repo-wide.)
- **Tooling:** tests are top-level `tests/test_*.py`, run `python -m pytest -q`. Lint `uvx ruff@0.16.8 check` / `format` (0 findings). JS tests `node --test tests/js/`. Squash-merge, one PR against `main`.
- **Report schema stability:** the U report dict has the SAME shape as the C report (`{anchor, reference{freq_hz,level_dbfs,window_ms}, taps[], static_gains}`) so `apps.audio.diagnostics.build_run_report` augments it unchanged (`stages`, `verdict`). C tap `computed=False`, D tap `computed=True` (projected C + sink_volume_db).

## Review Focus

- **gst never produces PCM** (unresolved node, pipeline dies, no frames decoded): `read_measurement` must time out, yield a C-silent tap (`silent=True, rms=None`) → verdict "no signal at C — inject/agent path broken", and the bridge/proc/fd/port must be cleaned up without hanging the WS loop. → Task 2 (bridge timeout+cleanup), Task 5 (silent→report+teardown).
- **Second U arrives while one is running / PTT active mid-request:** must be refused with a `busy` flag → orchestrator `StationBusy` → REST 409, and the in-flight run must be unaffected. → Task 5 (engine busy guards), Task 7 (orchestrator), Task 8 (409).
- **Agent WS disconnects mid-U run:** `engine.stop()` must tear down an in-flight diagnostic bridge + cancel the measurement task (no leaked gst/port). → Task 5.
- **Malformed / short reference media frames fed:** `on_media_frame` already drops frames failing `parse_frame`; a diagnostic in flight must tolerate junk frames and still measure from the valid ones (never raise into the WS loop). → Task 5.
- **Stream-ref collision:** the diagnostic reference frames must use a dedicated `DIAG_STREAM_REF` that cannot collide with a real `slotN.rx`/`op.mic` ref, and `on_media_frame` must only route those to the diagnostic bridge (not to a live TX). → Task 1 (constant), Task 5 (routing), Task 7 (orchestrator packs with it).

---

### Task 1: Diagnostic stream-ref constant + shared C/D tap builder (DRY)

**Files:**
- Modify: `station_agent/audio/diagnostics.py`
- Test: `tests/test_audio_diagnostics_dsp.py` (extend)

**Interfaces:**
- Produces: `DIAG_STREAM_REF: int = 0xFFFE` (module constant). `build_cd_taps(c_rms: float|None, c_peak: float|None, c_silent: bool, gains: dict, rate: int, window_ms: int) -> list[dict]` — returns `[C_tap, D_tap]` applying the existing D-projection rules (silent→D silent; `sink_volume_linear==0`→D silent "sink muted"; `sink_volume_db` present→D = C+sink_db; else→D None "sink volume unavailable"). C tap `computed=False`, D tap `computed=True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_diagnostics_dsp.py  (add)
from station_agent.audio import diagnostics as d

def test_diag_stream_ref_is_reserved_high_value():
    assert d.DIAG_STREAM_REF == 0xFFFE  # cannot collide with small real slot/mic refs

def test_build_cd_taps_projects_d_from_sink_db():
    gains = {"sink_volume_linear": 0.40, "sink_volume_db": -7.96}
    taps = d.build_cd_taps(-20.0, -20.0, False, gains, 16000, 300)
    assert [t["point"] for t in taps] == ["C", "D"]
    assert taps[0]["computed"] is False and taps[0]["silent"] is False
    assert taps[1]["computed"] is True
    assert taps[1]["rms_dbfs"] == -27.96 and taps[1]["peak_dbfs"] == -27.96

def test_build_cd_taps_silent_c_makes_d_silent():
    taps = d.build_cd_taps(None, None, True, {"sink_volume_linear": 0.40, "sink_volume_db": -7.96}, 16000, 300)
    assert taps[1]["silent"] is True and taps[1]["rms_dbfs"] is None

def test_build_cd_taps_muted_sink_makes_d_silent():
    taps = d.build_cd_taps(-20.0, -20.0, False, {"sink_volume_linear": 0.0, "sink_volume_db": None}, 16000, 300)
    assert taps[1]["silent"] is True and taps[1].get("note") == "sink muted"

def test_build_cd_taps_unreadable_sink_makes_d_unavailable():
    taps = d.build_cd_taps(-20.0, -20.0, False, {"sink_volume_linear": None, "sink_volume_db": None}, 16000, 300)
    assert taps[1]["silent"] is False and taps[1]["rms_dbfs"] is None
    assert "unavailable" in taps[1]["note"]
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_audio_diagnostics_dsp.py -q`
Expected: FAIL (`AttributeError: ... DIAG_STREAM_REF` / `build_cd_taps`).

- [ ] **Step 3: Implement**

Add near the other constants in `diagnostics.py`:

```python
#: Dedicated stream_ref for server-originated U-anchor reference frames. A high
#: sentinel so it can never collide with a real slotN.rx / op.mic ref (small,
#: assigned ascending from 0). The engine routes only this ref to the diagnostic
#: bridge; it is never a live TX stream.
DIAG_STREAM_REF: int = 0xFFFE
```

Add the helper (lift the D-projection block out of `run_diagnostic`):

```python
def build_cd_taps(c_rms, c_peak, c_silent, gains: dict, rate: int, window_ms: int) -> list[dict]:
    """Build the [C, D] tap list from a measured C plus the static sink gain.

    C is the real measurement (``computed=False``); D is projected D = C + sink
    volume (``computed=True``) per spec §3. Mirrors the rules previously inline in
    ``run_diagnostic`` so anchor C and anchor U produce an identical schema.
    """
    taps = [_tap("C", rate, c_rms, c_peak, c_silent, window_ms, computed=False)]
    linear = gains.get("sink_volume_linear")
    sink_db = gains.get("sink_volume_db")
    if c_silent:
        taps.append(_tap("D", rate, None, None, True, window_ms, computed=True))
    elif linear == 0:
        taps.append(_tap("D", rate, None, None, True, window_ms, computed=True, note="sink muted"))
    elif sink_db is not None:
        taps.append(_tap("D", rate, round(c_rms + sink_db, 2), round(c_peak + sink_db, 2),
                         False, window_ms, computed=True))
    else:
        taps.append(_tap("D", rate, None, None, False, window_ms, computed=True,
                         note="sink volume unavailable — D not projected"))
    return taps
```

Then refactor `run_diagnostic`'s anchor-C block to replace its inline D logic with:
`taps = build_cd_taps(c_rms, c_peak, c_silent, gains, rate, REF_WINDOW_MS)`.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audio_diagnostics_dsp.py tests/test_audio_diagnostics_agent.py -q`
Expected: PASS (new DSP tests + the existing agent C tests still green after the refactor).

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/diagnostics.py tests/test_audio_diagnostics_dsp.py
git commit -m "refactor(agent): extract build_cd_taps + add DIAG_STREAM_REF for anchor U"
```

---

### Task 2: `MeasuredTxBridge` — non-blocking diagnostic TX bridge

**Files:**
- Modify: `station_agent/audio/diagnostics.py`
- Test: `tests/test_audio_diag_measured_bridge.py` (create)

**Interfaces:**
- Consumes: `build_measured_tx_argv`, `_default_spawn`, `_default_read_measfd`, `rms_peak_dbfs` (this module); `station_agent.audio.rtp.wrap_rtp`.
- Produces: `class MeasuredTxBridge(tx_node: str, port: int, rate: int, *, spawn=_default_spawn, read_measfd=_default_read_measfd, socket_factory=<udp>, ssrc=0x5852_5841)` with methods: `start() -> None`, `feed_opus(payload: bytes) -> None`, `read_measurement(nbytes: int, timeout: float) -> bytes`, `stop() -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_diag_measured_bridge.py
import os
import station_agent.audio.diagnostics as d


class _FakeSock:
    def __init__(self): self.sent = []
    def sendto(self, data, addr): self.sent.append((data, addr))
    def close(self): pass


class _FakeProc:
    def __init__(self): self._alive = True
    def poll(self): return None if self._alive else 0
    def terminate(self): self._alive = False
    def wait(self, timeout=None): return 0
    def kill(self): self._alive = False


def test_measured_bridge_spawns_measured_tx_argv_and_reads_pipe():
    captured = {}

    def fake_spawn(make_argv):
        r, w = os.pipe()
        captured["argv"] = make_argv(w)
        os.write(w, b"\x01\x00" * 160)  # 160 S16LE samples
        os.close(w)
        return _FakeProc(), r

    br = d.MeasuredTxBridge("tx.node", 47050, 16000,
                            spawn=fake_spawn, socket_factory=_FakeSock)
    br.start()
    assert "gst-launch-1.0" in captured["argv"] and "udpsrc" in captured["argv"]
    assert "pipewiresink" in captured["argv"] and "fdsink" in captured["argv"]
    pcm = br.read_measurement(320, timeout=1.0)
    assert len(pcm) == 320
    br.stop()


def test_measured_bridge_feed_opus_wraps_rtp_to_port():
    sock = _FakeSock()
    def fake_spawn(make_argv):
        r, _w = os.pipe(); os.close(_w); return _FakeProc(), r
    br = d.MeasuredTxBridge("tx.node", 47051, 16000,
                            spawn=fake_spawn, socket_factory=lambda: sock)
    br.start()
    br.feed_opus(b"\xfc\xff\xfe")
    assert len(sock.sent) == 1
    datagram, addr = sock.sent[0]
    assert addr == ("127.0.0.1", 47051)
    assert len(datagram) > 3  # RTP header prepended
    br.stop()


def test_measured_bridge_feed_before_start_is_safe():
    br = d.MeasuredTxBridge("tx.node", 47052, 16000, socket_factory=_FakeSock)
    br.feed_opus(b"\x00")  # must not raise (no socket yet)
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_audio_diag_measured_bridge.py -q`
Expected: FAIL (`AttributeError: ... MeasuredTxBridge`).

- [ ] **Step 3: Implement**

Add to `diagnostics.py` (after the argv builders; import `socket`, `from station_agent.audio import rtp` at top; reuse existing `_RTP_PT`, `_LOOPBACK`):

```python
class MeasuredTxBridge:
    """Anchor-U diagnostic TX bridge: receives op.mic Opus frames over UDP loopback,
    injects them into *tx_node* (output_MONO = C), and taps the post-decode PCM via an
    fdsink measurement pipe. Non-blocking — ``feed_opus`` is called from the WS loop as
    reference frames arrive; the captured PCM is read separately via ``read_measurement``.

    RF SAFETY: writes only to the PipeWire sink; never keys the SA818.
    """

    def __init__(self, tx_node, port, rate, *, spawn=_default_spawn,
                 read_measfd=_default_read_measfd, socket_factory=None, ssrc=0x5852_5841):
        self._node = tx_node
        self._port = port
        self._rate = rate
        self._spawn = spawn
        self._read_measfd = read_measfd
        self._socket_factory = socket_factory or (lambda: __import__("socket").socket(
            __import__("socket").AF_INET, __import__("socket").SOCK_DGRAM))
        self._ssrc = ssrc
        self._proc = None
        self._sock = None
        self._read_fd = None
        self._seq = 0
        self._ts = 0

    def start(self):
        self._sock = self._socket_factory()
        make_argv = lambda mfd: build_measured_tx_argv(self._node, self._port, self._rate, meas_fd=mfd)  # noqa: E731
        self._proc, self._read_fd = self._spawn(make_argv)

    def feed_opus(self, payload: bytes) -> None:
        if self._sock is None:
            _log.debug("diag-bridge: feed before start; dropping")
            return
        datagram = rtp.wrap_rtp(payload, seq=self._seq, ts=self._ts, ssrc=self._ssrc, pt=_RTP_PT)
        try:
            self._sock.sendto(datagram, (_LOOPBACK, self._port))
        except OSError as exc:
            _log.debug("diag-bridge: sendto failed: %s", exc)
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + 960) & 0xFFFFFFFF  # RTP 48 kHz clock, 20 ms frame

    def read_measurement(self, nbytes: int, timeout: float) -> bytes:
        if self._read_fd is None:
            return b""
        pcm = self._read_measfd(self._read_fd, nbytes, timeout)
        self._read_fd = None  # read_measfd closes it
        return pcm

    def stop(self) -> None:
        _terminate_proc(self._proc)
        self._proc = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._read_fd is not None:
            try:
                _os.close(self._read_fd)
            except OSError:
                pass
            self._read_fd = None
```

(Add `import socket` and `from station_agent.audio import rtp` to the imports; drop the inline `__import__` once `socket` is imported — the default factory becomes `lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)`.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audio_diag_measured_bridge.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/diagnostics.py tests/test_audio_diag_measured_bridge.py
git commit -m "feat(agent): MeasuredTxBridge — non-blocking anchor-U diagnostic TX bridge"
```

---

### Task 3: `finish_u_diagnostic` — read measurement → U report dict

**Files:**
- Modify: `station_agent/audio/diagnostics.py`
- Test: `tests/test_audio_diag_measured_bridge.py` (extend)

**Interfaces:**
- Consumes: `MeasuredTxBridge` (duck-typed via `read_measurement`), `collect_static_gains`, `rms_peak_dbfs`, `build_cd_taps`, `REF_*`.
- Produces: `finish_u_diagnostic(bridge, *, backend, slot: int, signal: dict, rate: int) -> dict` returning `{anchor:"U", reference:{freq_hz,level_dbfs,window_ms}, taps:[C,D], static_gains}` — same schema as `run_diagnostic`'s C report.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_diag_measured_bridge.py  (add)
import math
import struct
import station_agent.audio.diagnostics as d


class _FakeBackend:
    def tx_sink_node(self, slot): return "sink.node"
    def get_volume(self, node): return 0.40


def _sine_pcm(level_dbfs, n=16000, rate=16000, freq=1000):
    amp = (10 ** (level_dbfs / 20)) * 32767
    return struct.pack(f"<{n}h", *[max(-32768, min(32767, int(round(amp * math.sin(2*math.pi*freq*i/rate))))) for i in range(n)])


class _CannedBridge:
    def __init__(self, pcm): self._pcm = pcm
    def read_measurement(self, nbytes, timeout): return self._pcm[:nbytes]


def test_finish_u_builds_report_with_c_measured_and_d_projected():
    pcm = _sine_pcm(-20.0)
    rep = d.finish_u_diagnostic(_CannedBridge(pcm), backend=_FakeBackend(),
                                slot=0, signal={"freq_hz": 1000, "level_dbfs": -20.0}, rate=16000)
    assert rep["anchor"] == "U"
    pts = {t["point"]: t for t in rep["taps"]}
    assert pts["C"]["computed"] is False and pts["C"]["silent"] is False
    assert pts["C"]["peak_dbfs"] == -20.0 or abs(pts["C"]["peak_dbfs"] + 20.0) < 0.2
    assert pts["D"]["computed"] is True
    assert abs(pts["D"]["rms_dbfs"] - (pts["C"]["rms_dbfs"] - 7.96)) < 0.05
    assert rep["static_gains"]["sink_volume_db"] == -7.96


def test_finish_u_silent_capture_reports_c_silent():
    rep = d.finish_u_diagnostic(_CannedBridge(b""), backend=_FakeBackend(),
                                slot=0, signal={}, rate=16000)
    pts = {t["point"]: t for t in rep["taps"]}
    assert pts["C"]["silent"] is True and pts["D"]["silent"] is True
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_audio_diag_measured_bridge.py -q`
Expected: FAIL (`AttributeError: ... finish_u_diagnostic`).

- [ ] **Step 3: Implement**

```python
def finish_u_diagnostic(bridge, *, backend, slot, signal, rate) -> dict:
    """Read the U diagnostic bridge's PCM tap and assemble the run report.

    Reads SETTLE+WINDOW ms of PCM, measures the trailing WINDOW ms at C, projects D
    from the static sink volume. Blocking (fd read) — call off the event loop.
    """
    freq = int(signal.get("freq_hz", REF_FREQ_HZ))
    level = float(signal.get("level_dbfs", REF_LEVEL_DBFS))
    nbytes = int(rate * (REF_SETTLE_MS + REF_WINDOW_MS) / 1000) * 2
    # Generous timeout: gst spawn + jitterbuffer latency before PCM flows.
    pcm = bridge.read_measurement(nbytes, (REF_SETTLE_MS + REF_WINDOW_MS) / 1000 + 3.0)
    win_bytes = int(rate * REF_WINDOW_MS / 1000) * 2
    window = pcm[-win_bytes:] if len(pcm) > win_bytes else pcm
    c_rms, c_peak, c_silent = rms_peak_dbfs(window)
    gains = collect_static_gains(backend, slot)
    taps = build_cd_taps(c_rms, c_peak, c_silent, gains, rate, REF_WINDOW_MS)
    return {
        "anchor": "U",
        "reference": {"freq_hz": freq, "level_dbfs": level, "window_ms": REF_WINDOW_MS},
        "taps": taps,
        "static_gains": gains,
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audio_diag_measured_bridge.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/diagnostics.py tests/test_audio_diag_measured_bridge.py
git commit -m "feat(agent): finish_u_diagnostic — assemble U report from the measured tap"
```

---

### Task 4: `BridgeFactory.make_diag_u` — port-managed diagnostic bridge

**Files:**
- Modify: `station_agent/audio/bridge_factory.py`
- Test: `tests/test_audio_bridge_factory.py` (create)

**Interfaces:**
- Consumes: `MeasuredTxBridge` (from `diagnostics`), `PortAllocator`.
- Produces: `BridgeFactory.make_diag_u(node: str, rate: int) -> MeasuredTxBridge` — a port-bound bridge that releases its UDP port to the allocator on `stop()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_bridge_factory.py
from station_agent.audio.bridge_factory import BridgeFactory


def test_make_diag_u_acquires_and_releases_port():
    f = BridgeFactory(port_base=48000)
    br = f.make_diag_u("tx.node", 16000)
    assert br._port == 48000           # first acquired port
    # same allocator → a second acquire must not reuse 48000 until released
    br2 = f.make_diag_u("tx.node", 16000)
    assert br2._port == 48001
    br.stop()                           # releases 48000
    br3 = f.make_diag_u("tx.node", 16000)
    assert br3._port == 48000           # reused after release
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_audio_bridge_factory.py -q`
Expected: FAIL (`AttributeError: ... make_diag_u`).

- [ ] **Step 3: Implement**

In `bridge_factory.py`:

```python
    def make_diag_u(self, node: str, rate: int):
        from station_agent.audio.diagnostics import MeasuredTxBridge  # local: avoid import cycle
        port = self._ports.acquire()
        return _PortBoundDiagU(node, port, rate, self._ports)


class _PortBoundDiagU:
    """MeasuredTxBridge wrapper that returns its UDP port to the allocator on stop."""

    def __init__(self, node, port, rate, ports):
        from station_agent.audio.diagnostics import MeasuredTxBridge
        self._impl = MeasuredTxBridge(node, port, rate)
        self._ports = ports
        self._port = port

    def start(self): self._impl.start()
    def feed_opus(self, payload): self._impl.feed_opus(payload)
    def read_measurement(self, nbytes, timeout): return self._impl.read_measurement(nbytes, timeout)

    def stop(self):
        self._impl.stop()
        self._ports.release(self._port)
```

(Composition rather than subclassing `MeasuredTxBridge` keeps `_port` introspectable and the allocator release independent of the impl.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audio_bridge_factory.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/bridge_factory.py tests/test_audio_bridge_factory.py
git commit -m "feat(agent): BridgeFactory.make_diag_u with port management"
```

---

### Task 5: Engine — non-blocking U orchestration, routing, busy guards, teardown

**Files:**
- Modify: `station_agent/audio/engine.py`
- Test: `tests/test_audio_diag_engine.py` (extend)

**Interfaces:**
- Consumes: `BridgeFactory.make_diag_u`, `diagnostics.finish_u_diagnostic`, `diagnostics.DIAG_STREAM_REF`.
- Produces: `AudioEngine.on_diag_command` now returns `None` for an accepted U run (result emitted later via `emit_json`); returns `{...,"error":...,"busy":True}` when refused busy. New private state `self._diag` / `self._diag_task`. `on_media_frame` routes `DIAG_STREAM_REF` frames to the diagnostic bridge. `stop()` tears down an in-flight diagnostic.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audio_diag_engine.py  (add; follow existing fake-factory/backend style in this file)
import asyncio
import pytest
from station_agent.audio.engine import AudioEngine
from station_agent.audio import diagnostics, frame


class _FakeDiagBridge:
    def __init__(self): self.fed = []; self.started = False; self.stopped = False
    def start(self): self.started = True
    def feed_opus(self, p): self.fed.append(p)
    def read_measurement(self, nbytes, timeout): return b"\x00\x10" * (nbytes // 2)
    def stop(self): self.stopped = True


class _FakeFactory:
    def __init__(self, diag): self._diag = diag
    def make_rx(self, *a, **k): raise AssertionError("unused")
    def make_tx(self, *a, **k):
        class _B:
            def start(self): pass
            def feed_opus(self, p): pass
            def stop(self): pass
        return _B()
    def make_diag_u(self, node, rate): return self._diag


class _FakeBackend:
    def list_audio_slots(self): return [0]
    def resolve_node(self, slot, direction): return "tx.node"
    def tx_sink_node(self, slot): return "sink.node"
    def get_volume(self, node): return 0.40


def _engine(diag):
    emitted = []
    async def emit_json(m): emitted.append(m)
    eng = AudioEngine(_FakeBackend(), emit_json=emit_json, emit_binary=lambda b: None,
                      bridge_factory=_FakeFactory(diag))
    return eng, emitted


@pytest.mark.asyncio
async def test_u_diag_is_nonblocking_and_emits_result():
    diag = _FakeDiagBridge()
    eng, emitted = _engine(diag)
    ret = await eng.on_diag_command({"request_id": "r1", "anchor": "U", "slot": 0, "signal": {}})
    assert ret is None                        # non-blocking: no synchronous result
    assert diag.started is True
    # feed a reference frame via the normal media path
    f = frame.pack_frame(stream_ref=diagnostics.DIAG_STREAM_REF, seq=0, ts=0, flags=0, payload=b"\xfc\xff")
    await eng.on_media_frame(f)
    assert diag.fed == [b"\xfc\xff"]
    # let the background measurement task finish
    await eng._diag_task
    assert diag.stopped is True
    res = [m for m in emitted if m.get("type") == "diag_result"]
    assert res and res[0]["request_id"] == "r1" and res[0]["anchor"] == "U"
    assert {t["point"] for t in res[0]["taps"]} == {"C", "D"}


@pytest.mark.asyncio
async def test_u_refused_when_tx_active_is_busy():
    diag = _FakeDiagBridge()
    eng, _ = _engine(diag)
    eng._tx = {"bridge": object(), "slot": 0, "module": "fm"}   # simulate PTT up
    ret = await eng.on_diag_command({"request_id": "r2", "anchor": "U", "slot": 0, "signal": {}})
    assert ret["busy"] is True and "TX active" in ret["error"]
    assert diag.started is False


@pytest.mark.asyncio
async def test_second_u_while_running_is_busy():
    diag = _FakeDiagBridge()
    eng, _ = _engine(diag)
    await eng.on_diag_command({"request_id": "r3", "anchor": "U", "slot": 0, "signal": {}})
    ret = await eng.on_diag_command({"request_id": "r4", "anchor": "U", "slot": 0, "signal": {}})
    assert ret["busy"] is True
    await eng._diag_task


@pytest.mark.asyncio
async def test_diag_ref_frame_ignored_when_no_diag_active():
    diag = _FakeDiagBridge()
    eng, _ = _engine(diag)
    f = frame.pack_frame(stream_ref=diagnostics.DIAG_STREAM_REF, seq=0, ts=0, flags=0, payload=b"x")
    await eng.on_media_frame(f)      # no diag running → silently ignored, no crash
    assert diag.fed == []


@pytest.mark.asyncio
async def test_stop_tears_down_inflight_diag():
    diag = _FakeDiagBridge()
    eng, _ = _engine(diag)
    # make the measurement block so the run is still in-flight at stop()
    import threading
    gate = threading.Event()
    diag.read_measurement = lambda n, t: (gate.wait(5), b"\x00\x10" * (n // 2))[1]
    await eng.on_diag_command({"request_id": "r5", "anchor": "U", "slot": 0, "signal": {}})
    await eng.stop()
    gate.set()
    assert diag.stopped is True
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_audio_diag_engine.py -q`
Expected: FAIL (U returns an error dict today, no `_diag_task`, no routing).

- [ ] **Step 3: Implement**

In `AudioEngine.__init__` add: `self._diag: dict | None = None` and `self._diag_task: asyncio.Task | None = None`.

Rewrite `on_diag_command` so the shared validation runs first, then branch:

```python
    async def on_diag_command(self, command: dict):
        from station_agent.audio import diagnostics
        rid = command.get("request_id")
        anchor = command.get("anchor")
        slot = command.get("slot")
        signal = command.get("signal") or {}
        base = {"v": 1, "type": "diag_result", "request_id": rid}
        if anchor not in ("C", "U"):
            return {**base, "error": f"unsupported anchor {anchor!r}"}
        if not isinstance(slot, int) or isinstance(slot, bool):
            return {**base, "error": "slot must be an int"}
        # RF safety + exclusivity: refuse while a TX bridge (PTT/mic) is up.
        if self._tx is not None:
            return {**base, "busy": True,
                    "error": "refused: TX active — diagnostic inject would reach a keyed transmitter"}
        if anchor == "U":
            return await self._start_u_diagnostic(base, slot, signal)
        # anchor C — synchronous inject/measure (unchanged behaviour)
        try:
            report = await self._to_thread(
                lambda: diagnostics.run_diagnostic(
                    anchor="C", slot=slot, signal=signal,
                    backend=self._backend, rate=self.registry.mic_rate))
        except Exception as exc:  # noqa: BLE001
            logger.exception("engine: diagnostic run failed")
            return {**base, "error": f"{type(exc).__name__}: {exc}"}
        return {**base, **report}

    async def _start_u_diagnostic(self, base, slot, signal):
        if self._diag is not None:
            return {**base, "busy": True, "error": "refused: a diagnostic is already running"}
        node = await self._to_thread(self._backend.resolve_node, slot, "tx")
        if node is None:
            return {**base, "error": f"no TX node for slot {slot}"}
        bridge = self._factory.make_diag_u(node, self.registry.mic_rate)
        try:
            await self._to_thread(bridge.start)
        except Exception as exc:  # noqa: BLE001 — a failed start must release the port
            logger.exception("engine: U diagnostic bridge start failed")
            await self._to_thread(_safe_stop, bridge)
            return {**base, "error": f"U diagnostic bridge start failed: {exc}"}
        self._diag = {"bridge": bridge, "slot": slot}
        self._diag_task = asyncio.ensure_future(self._run_u_measurement(base, slot, signal, bridge))
        return None  # diag_result emitted asynchronously once measurement completes

    async def _run_u_measurement(self, base, slot, signal, bridge):
        from station_agent.audio import diagnostics
        try:
            report = await self._to_thread(
                lambda: diagnostics.finish_u_diagnostic(
                    bridge, backend=self._backend, slot=slot, signal=signal,
                    rate=self.registry.mic_rate))
            result = {**base, **report}
        except Exception as exc:  # noqa: BLE001 — never let a diag failure escape
            logger.exception("engine: U diagnostic measurement failed")
            result = {**base, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            await self._teardown_diag()
        await self._emit_json(result)
```

Add routing in `on_media_frame` BEFORE the existing TX gate:

```python
        if self._diag is not None and mf.stream_ref == frame.DIAG_STREAM_REF:  # see note below
            self._diag["bridge"].feed_opus(mf.payload)
            return
```

Routing note: reference `diagnostics.DIAG_STREAM_REF`. To avoid importing `diagnostics` at engine top-level (it imports `selftest`), add `from station_agent.audio.diagnostics import DIAG_STREAM_REF` inside `on_media_frame` is wasteful per-frame; instead import it once at engine module top (`from station_agent.audio.diagnostics import DIAG_STREAM_REF`) — verify no import cycle (diagnostics imports selftest, not engine). Use `DIAG_STREAM_REF` directly (not `frame.DIAG_STREAM_REF`).

Add teardown helper + call it from `stop()`:

```python
    async def _teardown_diag(self) -> None:
        task = self._diag_task
        self._diag_task = None
        diag = self._diag
        self._diag = None
        if diag is not None:
            await self._to_thread(_safe_stop, diag["bridge"])
        if task is not None and task is not asyncio.current_task():
            task.cancel()
```

In `stop()` add `await self._teardown_diag()` alongside the existing `_teardown_tx()`.

(Note `_teardown_diag` is called from inside `_run_u_measurement`'s `finally` — there `self._diag_task is current_task`, so we must NOT cancel ourselves; the `is not current_task()` guard handles it.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audio_diag_engine.py -q`
Expected: PASS (all U engine tests).

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/engine.py tests/test_audio_diag_engine.py
git commit -m "feat(agent): engine non-blocking anchor-U run, ref routing, busy guard, teardown"
```

---

### Task 6: ws_client — only send a synchronous diag_result

**Files:**
- Modify: `station_agent/audio/ws_client.py:168-170`
- Test: `tests/test_audio_diag_ws.py` (extend)

**Interfaces:**
- Consumes: `engine.on_diag_command` may now return `None`.
- Produces: `_dispatch` sends a `diag_result` only when `on_diag_command` returns a non-`None` dict (U emits its own result later via `emit_json`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_diag_ws.py  (add; match the existing fake-ws style in this file)
import json
import pytest
from station_agent.audio.ws_client import AudioClient


@pytest.mark.asyncio
async def test_dispatch_does_not_send_when_diag_returns_none(monkeypatch):
    sent = []

    client = AudioClient.__new__(AudioClient)   # bypass __init__ (no key/socket)
    client._ws = None
    client._shutdown = __import__("threading").Event()

    class _Eng:
        async def on_diag_command(self, msg): return None     # U accepted, async result
    client._engine = _Eng()

    async def _send_json(m): sent.append(m)
    client._send_json = _send_json

    await client._dispatch(json.dumps({"type": "diag_command", "anchor": "U", "request_id": "r1"}))
    assert sent == []   # nothing sent synchronously


@pytest.mark.asyncio
async def test_dispatch_sends_when_diag_returns_dict():
    sent = []
    client = AudioClient.__new__(AudioClient)
    client._ws = None
    client._shutdown = __import__("threading").Event()

    class _Eng:
        async def on_diag_command(self, msg): return {"type": "diag_result", "request_id": "r2"}
    client._engine = _Eng()

    async def _send_json(m): sent.append(m)
    client._send_json = _send_json

    await client._dispatch(json.dumps({"type": "diag_command", "anchor": "C", "request_id": "r2"}))
    assert sent == [{"type": "diag_result", "request_id": "r2"}]
```

(If the existing `test_audio_diag_ws.py` has a cleaner client-construction helper, reuse it instead of `__new__`.)

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_audio_diag_ws.py -q`
Expected: FAIL (`_dispatch` currently always sends the result, even `None`).

- [ ] **Step 3: Implement**

Change the `diag_command` branch in `_dispatch`:

```python
        elif mtype == "diag_command":
            result = await self._engine.on_diag_command(msg)
            if result is not None:
                await self._send_json(result)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audio_diag_ws.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/ws_client.py tests/test_audio_diag_ws.py
git commit -m "fix(agent): ws_client sends diag_result only for synchronous (anchor C) runs"
```

---

### Task 7: Orchestrator — DIAG_STREAM_REF + StationBusy

**Files:**
- Modify: `apps/audio/orchestrator.py`
- Test: `tests/test_audio_diag_orchestrator.py` (extend)

**Interfaces:**
- Consumes: agent reply may carry `{"busy": True}`.
- Produces: `class StationBusy(Exception)`. `run_headless_diagnostic` packs U reference frames with `station_agent.audio.diagnostics.DIAG_STREAM_REF` (not the `OP_MIC_DIAG_REF = 0` stub) and raises `StationBusy` when the agent reply has `busy`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_diag_orchestrator.py  (add; reuse the file's existing channel-layer fakes)
import pytest
from apps.audio import orchestrator
from station_agent.audio.diagnostics import DIAG_STREAM_REF


@pytest.mark.asyncio
async def test_u_frames_use_diag_stream_ref(monkeypatch, ...):   # wire up the file's online-station + fake layer
    # Arrange a fake channel layer that records audio.ref_media payloads, and a station
    # whose status == "online"; stub the reply to a minimal agent_report.
    # Act: await orchestrator.run_headless_diagnostic(station_id, "U", signal)
    # Assert: every streamed media frame parses with stream_ref == DIAG_STREAM_REF.
    from station_agent.audio import frame
    for data in recorded_ref_media:
        assert frame.parse_frame(data).stream_ref == DIAG_STREAM_REF


@pytest.mark.asyncio
async def test_busy_reply_raises_station_busy(...):
    # reply envelope msg = {"busy": True, "error": "refused: TX active ..."}
    with pytest.raises(orchestrator.StationBusy):
        await orchestrator.run_headless_diagnostic(station_id, "U", signal)
```

(Fill the `...` from the existing fixtures already in `tests/test_audio_diag_orchestrator.py`; the assertions above are the new behaviour under test.)

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_audio_diag_orchestrator.py -q`
Expected: FAIL (frames use ref 0; no `StationBusy`).

- [ ] **Step 3: Implement**

In `orchestrator.py`:
- Add exception: `class StationBusy(Exception): ...` (with a docstring, `# noqa: N818`).
- Replace the `OP_MIC_DIAG_REF` usage in the U streaming loop with the imported `DIAG_STREAM_REF`:
  `from station_agent.audio.diagnostics import DIAG_STREAM_REF` and
  `iter_media_frames(frames, stream_ref=DIAG_STREAM_REF, repeat=REF_REPEAT)`.
  (Remove the now-unused `OP_MIC_DIAG_REF` constant, or keep it re-aliased to `DIAG_STREAM_REF` — prefer removal; grep for other refs first.)
- In the reply-handling block, before the `error` check:

```python
    agent_report = envelope["msg"]
    if agent_report.get("busy"):
        raise StationBusy()
    if agent_report.get("error"):
        return {"anchor": anchor, "error": agent_report["error"]}
```

- Update the module docstring's "anchor U … currently rejected at the REST endpoint (400)" note to reflect that U is now live.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audio_diag_orchestrator.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/audio/orchestrator.py tests/test_audio_diag_orchestrator.py
git commit -m "feat(server): orchestrator streams U ref with DIAG_STREAM_REF, raises StationBusy"
```

---

### Task 8: REST — un-gate U, return 409 when busy

**Files:**
- Modify: `apps/api/diagnostics_views.py`
- Test: `tests/test_audio_diag_endpoint.py` (extend)
- Modify: `docs/audio-diagnostics-usage.md`

**Interfaces:**
- Consumes: `orchestrator.StationBusy`.
- Produces: `POST /api/v1/stations/{pk}/audio-diagnostics/` accepts `anchor:"U"`; returns 409 `{"detail": "station busy"}` on `StationBusy`; existing 400/403/404/503/504 unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_diag_endpoint.py  (add; reuse the file's auth/station fixtures + orchestrator monkeypatch)
def test_anchor_u_is_accepted_and_runs(self):
    # monkeypatch orchestrator.run_headless_diagnostic to return a canned U report
    resp = self._post({"anchor": "U"})
    assert resp.status_code == 200
    assert resp.json()["anchor"] == "U"

def test_busy_returns_409(self):
    def _busy(*a, **k): raise orchestrator.StationBusy()
    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", _busy)  # async_to_sync wraps it
    resp = self._post({"anchor": "U"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "station busy"

def test_anchor_z_still_400(self):
    resp = self._post({"anchor": "Z"})
    assert resp.status_code == 400
```

(The existing file already monkeypatches `run_headless_diagnostic` for the C/503/504 cases — mirror that mechanism; `StationBusy` must be raisable through the `async_to_sync` wrapper, so raise it synchronously from the patched coroutine like the existing `AgentNotConnected` test does.)

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_audio_diag_endpoint.py -q`
Expected: FAIL (U → 400; no 409 branch).

- [ ] **Step 3: Implement**

In `diagnostics_views.py`:
- `_VALID_ANCHORS = {"C", "U"}`.
- Delete the special `if anchor == "U": raise ValidationError(...)` block.
- Wrap the orchestrator call to also catch busy:

```python
        try:
            report = async_to_sync(orchestrator.run_headless_diagnostic)(
                station.pk, anchor, signal, slot=slot
            )
        except orchestrator.AgentNotConnected:
            return Response({"detail": "station agent not connected"}, status=503)
        except orchestrator.StationBusy:
            return Response({"detail": "station busy"}, status=409)
        except orchestrator.DiagnosticTimeout:
            return Response({"detail": "diagnostic timed out"}, status=504)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audio_diag_endpoint.py -q`
Expected: PASS.

- [ ] **Step 5: Update docs**

In `docs/audio-diagnostics-usage.md`: move anchor U from "experimental/gated" to a supported headless anchor; document the request (`{"anchor":"U"}`), the 409-when-busy response, the idle-station requirement, and that U measures the full digital chain (U→C→D) browserlessly. State RF-safety (no SA818 keying).

- [ ] **Step 6: Commit**

```bash
git add apps/api/diagnostics_views.py tests/test_audio_diag_endpoint.py docs/audio-diagnostics-usage.md
git commit -m "feat(api): un-gate anchor U, return 409 when station busy; docs"
```

---

### Task 9: On-station validation on test-station .211 (GATING — the point of U)

**Files:**
- Create (local, NOT committed): `/tmp/diag_u_onstation.py` validation driver.

**Not a code-change task — a mandatory validation gate.** CI-green is necessary but NOT sufficient; the on-station U measurement must really work (the prior build gated U precisely because it did not run on-station).

Validation drives the REAL agent-side U path on the test station (full station-agent + PipeWire 1.6.8 + GStreamer + fm_board), direct-driving the engine with the real committed Opus fixture — exercising real `opusdec`/resample/`output_MONO`/sink/ALSA. The server↔agent channel-layer transport is generic and covered by CI (Task 7). Access + fast-dev-loop details: project memory `infra/test-station-211`, `feature/audio-diag-anchor-u/progress`. **Do not commit station IP/SSH.**

- [ ] **Step 1: Deploy branch agent code to the station's fast-dev mount**

```bash
ssh root@<station> 'mountpoint -q /mnt/dev || mount -t tmpfs tmpfs /mnt/dev'
rsync -a --delete <repo>/station_agent/ root@<station>:/mnt/dev/station_agent/
scp <repo>/apps/audio/data/ref_1khz_-20dbfs_16k.opusframes root@<station>:/tmp/ref.opusframes
ssh root@<station> 'STATION_AGENT_DEV_DRYRUN=1 /usr/bin/station-agent-dev-launch'  # expect: mount:/mnt/dev/station_agent
```

- [ ] **Step 2: Run the direct-drive U validation**

Driver (`/tmp/diag_u_onstation.py`, run on the station) builds the real backend + engine, injects the fixture through the real U path, prints the report:

```python
import asyncio, json, struct
from station_agent.audio.engine import AudioEngine
from station_agent.audio.router_backend import PipeWireRouterBackend
from station_agent.audio import frame, diagnostics

def load_frames(path):
    data = open(path, "rb").read(); out = []; off = 0
    while off + 2 <= len(data):
        (n,) = struct.unpack_from(">H", data, off); off += 2
        if off + n > len(data): break
        out.append(data[off:off+n]); off += n
    return out

async def main(slot):
    emitted = []
    eng = AudioEngine(PipeWireRouterBackend(), emit_json=lambda m: emitted.append(m) or asyncio.sleep(0),
                      emit_binary=lambda b: None)
    await eng.start()
    ret = await eng.on_diag_command({"request_id": "val", "anchor": "U", "slot": slot,
                                     "signal": {"freq_hz": 1000, "level_dbfs": -20.0}})
    assert ret is None, ret
    frames = load_frames("/tmp/ref.opusframes")
    seq = 0
    for _ in range(3):
        for p in frames:
            await eng.on_media_frame(frame.pack_frame(stream_ref=diagnostics.DIAG_STREAM_REF,
                                                       seq=seq, ts=seq*320, flags=0, payload=p))
            seq += 1
    await eng._diag_task
    print(json.dumps([m for m in emitted if m.get("type") == "diag_result"][-1], indent=2))

import sys; asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 0))
```

```bash
# stop the service first so only ONE PipeWire consumer binds the TX node during the run,
# then run the driver as the agent user with the agent's PipeWire runtime env:
ssh root@<station> 'systemctl stop station-agent;
  XDG_RUNTIME_DIR=/run/pipewire PIPEWIRE_RUNTIME_DIR=/run/pipewire \
  PYTHONPATH=/mnt/dev /usr/bin/python3 /tmp/diag_u_onstation.py <tx_slot>;
  systemctl start station-agent'
```

(Find `<tx_slot>` from `list_audio_slots()` / the FM board's `OE5XRX_SLOT` tag. The live sink volume is read by the agent — do not hardcode; node 59 "FM Transceiver Board Mono" was 0.40 at plan time.)

- [ ] **Step 3: Assert the measurement is plausible**

Expected report: `anchor:"U"`, C tap `rms/peak ≈ −20 dBFS` (`silent:false, computed:false`), D tap `= C + sink_volume_db` (`computed:true`), `static_gains.sink_volume_db ≈ −7.96` (if sink still 0.40; if the user left it at 1.0, D≈C and sink_db≈0 — read live, assert against the reported sink_db, not a constant). Verdict references the sink stage. If C is silent → the U path is NOT working on-station: debug (feed order, gst startup race, node resolution) and iterate — **do not** claim done.

- [ ] **Step 4: Record + clean up**

Record the real numbers in `feature/audio-diag-anchor-u/progress`. Restore the station: `rsync`/dev-mount is ephemeral (tmpfs) — `umount /mnt/dev` (or leave empty), ensure `station-agent` is running the baked code again (`STATION_AGENT_DEV_DRYRUN=1 … ` → `baked` after unmount), station back `online`.

---

## Notes for the whole-branch review

- Run the full suite: `python -m pytest -q` and `node --test tests/js/` (JS unchanged here but keep green), then `uvx ruff@0.16.8 check` + `uvx ruff@0.16.8 format --check`.
- Confirm no leaked `OP_MIC_DIAG_REF` references remain after Task 7 (`grep -rn OP_MIC_DIAG_REF`).
- Confirm the U report flows through `build_run_report` unchanged (anchor-agnostic): the orchestrator returns `build_run_report(agent_report)` for both C and U.
