# station_agent Audio Pipeline — Component Design (Session B)

**Status:** Draft for review
**Date:** 2026-09-05
**Parent:** `docs/superpowers/specs/2026-09-03-audio-subsystem-design.md` (Spec 0 — NORMATIVE wire contract)
**Repo:** `station-manager` (`station_agent`), Python 3.13, asyncio
**Scope:** Session B only — the agent side. No server/Django-Channels (Session C), no web (Session D).

This spec resolves Spec 0 §10 (open implementation decisions) for the agent and defines
the component boundaries, interfaces, and test strategy. It does **not** re-specify the
wire contract — Spec 0 §5 is authoritative and this design conforms to it byte-for-byte so
Session C can build independently against the same §5.7 fixtures.

---

## 1. §10 Decisions (locked)

| Spec 0 §10 item | Decision | Rationale |
|---|---|---|
| Bridge encoder | **GStreamer via `gst-launch-1.0` subprocess** | No new image packages (A did not ship python-gi/gst-python). Behind a bridge interface. |
| Graph control | **`pw-cli`/`pw-dump`/`pw-link`/`wpctl` subprocess**, behind `RouterBackend` | MVP-robust, no libpipewire bindings, no new deps. |
| MVP FM encode rate | **RX = 8 kHz NB** (module native); `op.mic` uplink stays **16 kHz WB** (browser-produced) | Matches real UAC2 (Spec 0 §12) + the §5 `advertise` fixture; no upsample-then-encode waste. |
| `slotN` audio device match | **Agent maps `slot → PipeWire node` via `OE5XRX_SLOT` udev tag → ALSA card index → `api.alsa.card`** (Spec 0 §12 Finding 2) | One mechanism for control + audio; sim and real resolve identically; no hardcoded per-port node names. |
| Discrete-packet transport out of `gst-launch` | **RTP-over-UDP loopback** (`rtpopuspay ! udpsink` / `udpsrc ! rtpopusdepay`) | Without an `appsink` (needs python-gi), a UDP datagram is a clean self-delimiting boundary = exactly one Opus packet. RTP header parse/emit is pure + unit-testable. |
| Selftest FFT | **Pure-Python Goertzel** (single-bin power), no numpy | numpy is not an image dep; Goertzel is the audio analog Spec 0 §8 already names for the TX probe. |
| Tone-shim home | Out of scope (Session A owns it) | — |

**No new `station_agent` runtime deps.** `pyproject.toml`/`requirements.txt` unchanged.
The runtime shells out to tools present in the A-image (`gst-launch-1.0`, `pw-*`, `wpctl`).
Unit tests inject fakes and never require those binaries, GStreamer, PipeWire, or numpy.

---

## 2. Package layout

New subpackage `station_agent/audio/` (mirrors the flat top-level style but grouped):

| Module | Purpose | I/O | Runtime deps |
|---|---|---|---|
| `audio/frame.py` | §5.3 media-frame header pack/parse | none (pure) | — |
| `audio/rtp.py` | Minimal RTP wrap/strip for the UDP bridge boundary | none (pure) | — |
| `audio/goertzel.py` | Single-frequency power detector for selftest | none (pure) | — |
| `audio/router_backend.py` | `RouterBackend` iface + `PipeWireRouterBackend` (slot→node, link, volume) | subprocess (`pw-*`/`wpctl`), sysfs | injected runner |
| `audio/opus_bridge.py` | `RxBridge`/`TxBridge` — gst-launch pipelines + UDP socket | subprocess + UDP | injected factory |
| `audio/streams.py` | Stream registry: `stream_id ↔ stream_ref`, format, demand state | none (pure) | — |
| `audio/engine.py` | `AudioEngine` — orchestrates registry + backend + bridges; builds `advertise`; demand-gating lifecycle; `mic_state`→TX; PTT/dead-man coupling | — | — |
| `audio/ws_client.py` | `AudioClient` — persistent Ed25519 outbound audio-WS (analog `control_client.py`) | WebSocket | websockets |
| `audio/router_module.py` | `audio-router` virtual control-plane module (§5.6): descriptor + `tx_route` handler | — | — |
| `audio/config.py` fields | added to `AgentConfig` (audio_enabled, udp base port, rates, dead-man) | — | — |
| `selftest.py::run_audio` | `selftest audio` command | subprocess | — |

Each module is independently testable; the only WS-touching module is `ws_client.py`, the
only device-touching modules are `router_backend.py` + `opus_bridge.py`, both with an
injectable subprocess/socket seam.

---

## 3. §5.3 media frame (`frame.py`)

Little-endian, fixed 12-byte header + raw Opus payload (Spec 0 §5.3):

```
0  u8   magic = 0xA5
1  u8   ver   = 1
2  u16  stream_ref
4  u16  seq         (per-stream, wraps 2^16)
6  u32  ts          (RTP-style, samples at stream rate)
10 u8   flags       (bit0 FEC-present, bit1 DTX/comfort, bit2 marker)
11 u8   reserved = 0
12 …    payload     (one 20 ms Opus packet)
```

- `FLAG_FEC = 0x01`, `FLAG_DTX = 0x02`, `FLAG_MARKER = 0x04`.
- `pack_frame(stream_ref, seq, ts, flags, payload) -> bytes` — masks seq/ts into range, raises on non-bytes payload.
- `parse_frame(data) -> MediaFrame` — validates magic+ver+length, raises `FrameError` (fail-closed) on bad magic/short buffer/unknown ver. `MediaFrame` = frozen dataclass (`stream_ref, seq, ts, flags, payload` + `fec/dtx/marker` bool props).

## 4. RTP boundary (`rtp.py`)

The UDP datagram between `gst-launch` and the agent carries exactly one RTP/Opus packet.
We only need the Opus payload; RTP seq/ts are the transport's, not §5.3's.

- `strip_rtp(datagram) -> bytes` — parse the 12-byte fixed RTP header (RFC 3550), skip CSRC
  list (`cc` count) and a header extension if the `X` bit is set; return the Opus payload.
  Fail-closed (raise `RtpError`) on truncated/invalid version.
- `wrap_rtp(payload, seq, ts, ssrc, pt=96, marker=False) -> bytes` — build a minimal RTP
  header (no CSRC, no extension) for TX injection into `rtpopusdepay`. `ts` advances 960 per
  20 ms frame (48 kHz RTP clock per RFC 7587, independent of the 8/16 kHz media rate); `seq`
  increments per packet; `pt`/`ssrc` fixed per stream.

## 5. RouterBackend (`router_backend.py`)

Interface (so a future libpipewire backend swaps in):

```python
class RouterBackend(Protocol):
    def resolve_node(self, slot: int, direction: str) -> str | None      # "rx"|"tx" -> node.name
    def list_audio_slots(self) -> list[int]                              # slots with an audio node
    def link(self, out_node: str, in_node: str) -> bool
    def unlink(self, out_node: str, in_node: str) -> bool
    def set_volume(self, node: str, linear: float) -> bool
```

`PipeWireRouterBackend`:
- **slot→node (Spec 0 §12 Finding 2):**
  1. `OE5XRX_SLOT` per sound device from sysfs — read `/sys/class/sound/card*/device/uevent`
     (or the udev-exported property) to map ALSA **card index → slot**. (Minimal path: parse
     `OE5XRX_SLOT=` out of the card device `uevent`; falls back to a udev query only if absent.)
  2. `pw-dump` → find the node whose `info.props["api.alsa.card"]` equals that card index and
     whose `media.class` is `Audio/Source` (rx) or `Audio/Sink` (tx). Use **`node.name`**, never
     the `wpctl` description (A's learning). RX source = `oe5xrx.slotN`, TX sink = `oe5xrx.slotN.tx`;
     resolution is by `api.alsa.card`, and the `node.name` is asserted to match the expected
     `oe5xrx.slotN[.tx]` for a clean error if WirePlumber naming drifts.
- `link`/`unlink` shell `pw-link` (port globs `<node>:*`); `set_volume` shells `wpctl set-volume`.
- All subprocess calls go through an injected `run(argv) -> CompletedProcess`-like callable
  (default wraps `subprocess.run` with timeout + `OSError` fail-closed). `pw-dump` JSON parse is
  isolated + tested against a captured fixture.

Never raises into the caller — a failed resolve returns `None`, a failed op returns `False`,
logged at debug (mirrors `slot_discovery`/`slot_control` hygiene).

## 6. Opus bridge (`opus_bridge.py`)

§5.4 profile: 20 ms frames, VBR, `application=voip`/`audio`, **in-band FEC on**, DTX allowed,
PLC at the receiver. Encode rate = negotiated stream rate.

**RX (tap module → Opus → callback):**
```
gst-launch-1.0 pipewiresrc target-object=<rx_node> ! audioconvert ! audioresample !
  audio/x-raw,rate=<rate>,channels=1 ! opusenc bitrate-type=vbr audio-type=voip
  frame-size=20 inband-fec=true dtx=true ! rtpopuspay pt=96 ! udpsink host=127.0.0.1 port=<p>
```
`RxBridge.start()` binds a UDP socket on `<p>` first, spawns the process, and runs a reader
that `strip_rtp` → `on_opus(payload, marker)`. `stop()` kills the process + closes the socket.

**TX (Opus ← WS → inject into module sink):**
```
gst-launch-1.0 udpsrc port=<p> caps="application/x-rtp,media=audio,clock-rate=48000,
  encoding-name=OPUS,payload=96" ! rtpjitterbuffer ! rtpopusdepay ! opusdec plc=true
  use-inband-fec=true ! audioconvert ! audioresample ! pipewiresink target-object=<tx_node>
```
`TxBridge.feed_opus(payload)` → `wrap_rtp` → `sendto(127.0.0.1,<p>)`. `rtpjitterbuffer` gives
the adaptive jitter buffer Spec 0 §2 mandates; `plc/inband-fec` give loss tolerance.

- Ports come from a per-stream allocator (base `udp_port_base` + stream index) to avoid
  collisions across concurrent streams.
- Pipeline **argv builders are pure functions** (`build_rx_argv`, `build_tx_argv`) and unit
  tested; the process/socket lifecycle uses an injected `spawn`/socket factory so tests run
  without GStreamer.
- Bridges are demand-gated: created on `source_subscribe`, torn down on `source_unsubscribe`
  (RX) and on `mic_state active=false` (TX).

## 7. Stream registry (`streams.py`)

- Sources advertised: one `slotN.rx` per audio slot (from `RouterBackend.list_audio_slots`,
  format `{rate:8000,channels:1}`) + `op.mic` (`{rate:16000,channels:1}`, browser-produced).
- Assigns a stable numeric `stream_ref` per `stream_id` at advertise time; `stream_ref↔stream_id`
  map is the single source of truth for framing. `op.mic` gets a ref too (agent receives its
  media for TX injection).
- Tracks per-source subscriber/demand state and the live/idle/error lifecycle for `stream_state`.

## 8. AudioEngine (`engine.py`)

Owns registry + backend + bridge factory. Pure of WS/asyncio transport specifics (takes a
`send_json`/`send_binary` pair, mirroring how `Broker` takes `send`). Responsibilities:

- **advertise:** enumerate slots → build the §5 `advertise` payload; re-advertise on hotplug.
- **demand gating:** `source_subscribe(stream_id)` → resolve rx node → start `RxBridge`, pump
  its Opus packets as §5.3 binary frames (`FLAG_FEC` set when opusenc emits FEC; `FLAG_MARKER`
  on talk-onset). `source_unsubscribe` → stop the bridge, emit `stream_state idle`.
- **mic_state:** `active=true, tx_slot=N` → resolve tx node, start `TxBridge`, arm the audio
  dead-man; incoming `op.mic` binary frames → `parse_frame` → `TxBridge.feed_opus`. `active=false`
  → stop TX, disarm. A dead-man timeout (no mic frame within `audio_dead_man_timeout`) tears TX
  down locally (defence-in-depth beside the server's lock/PTT gate and the control-plane PTT).
- **tx_route:** set/clear the current TX target module; `mic_state` uses it. This is the state
  the `audio-router` virtual module (§9) mutates.

## 9. audio-router virtual module (`router_module.py`, Spec 0 §5.6)

The router is a **virtual module on the existing control-plane**, not a new channel.
`Broker` gains a minimal, optional injection seam: a list of "virtual modules", each providing
`(synthetic_slot, module_id, descriptor)` for inventory and a `handle(op, capability, value)`
coroutine for commands. `AudioRouterModule`:

- Synthetic slot (e.g. `1000`, configurable, non-colliding with physical slots) + module id
  `audio-router`.
- Descriptor capabilities:
  - `streams` — telemetry, dynamic list of available audio streams (mirrors `advertise`).
  - `tx_route` — `kind:action`, `type:"route"` (a control-plane-internal value type: a
    `{slot,module}` object or `null` to clear), `op:set`. **Lock-gated by the server** (same
    path as every other write command; the agent executes what the server, which enforces the
    lock, forwards). Records the route; the authoritative mic-injection target reaches the
    agent via `mic_state` on the audio-WS, so no cross-thread call to the live engine is needed.
- **Non-preclusion hooks (Spec 0 §11):** `set_tx_route` accepts a non-human requester later
  (no hardcoded "browser lock holder"); the occupancy signal is out of scope but the seam is
  left. Not built now.

Broker changes are additive and guarded: zero virtual modules ⇒ byte-identical behaviour, so
existing control tests stay green.

## 10. Wiring & config

- `AgentConfig` gains: `audio_enabled: bool=False`, `audio_udp_port_base:int=47000`,
  `audio_rx_rate:int=8000`, `audio_mic_rate:int=16000`, `audio_dead_man_timeout:float=1.5`,
  `audio_slot_dev_base` reuse of `slot_dev_base`. All optional; default off.
- `agent.py` starts an `AudioClient` thread when `audio_enabled` (same pattern as the control
  client; local import so audio-off stations never import it). The audio-router virtual module is
  registered on the control `Broker` only when audio is enabled.
- Endpoint `wss://…/ws/agent/audio/<station_id>/`, Ed25519 query-param auth identical to control.

## 11. selftest (`selftest.py::run_audio`, Spec 0 §7/§8)

`python -m station_agent selftest audio [--slot 1] [--tx-freq 1500]`, on-target only
(needs the A-image PipeWire + GStreamer + sim tone shim):

1. **RX:** gst pipeline `pipewiresrc target-object=oe5xrx.slot<N> ! … ! opusenc … ! opusdec …
   ! audio/x-raw,format=S16LE,rate=8000,channels=1 ! fdsink`; read PCM from stdout for ~0.5 s;
   `goertzel(pcm, 1000, 8000)` power must dominate → exercises the real 8 k→48 k→8 k resample +
   Opus roundtrip. Green only if the 1 kHz bin peaks.
2. **TX:** synthesize a distinct tone (`--tx-freq`, default 1500 Hz) → opusenc → `TxBridge` →
   `pipewiresink target-object=oe5xrx.slot<N>.tx`; the sim's reverse cable exposes it on a
   capture (`oe5xrx.slot<N>` reverse or a dedicated probe node) → `goertzel(pcm, 1500, 8000)`
   must peak. Distinct freq so RX/TX can't be confused.

Exit non-zero on any failure with a clear log line. **Audio-boundary honesty rule:** the gate is
only truly green on real CM4/bench HW — sim-green is necessary, not sufficient (analog to the
serial-boundary rule in `station-manager/CLAUDE.md`).

## 12. Test strategy (TDD)

Unit (no GStreamer/PipeWire/numpy/opus needed):
- `frame.py`: pack/parse round-trip, bad magic/ver/length fail-closed, seq/ts wrap, flag bits;
  parse the golden `media_frame_slot0rx.bin` header.
- `rtp.py`: wrap/strip round-trip, CSRC + extension skip, truncated fail-closed.
- `goertzel.py`: synthetic sine peaks at its bin, rejects off-bin.
- `router_backend.py`: slot→node against a captured `pw-dump` JSON fixture (asserts resolution
  by `api.alsa.card`, not description; Finding-2 port-name node still resolves); link/volume argv;
  fail-closed on subprocess error.
- `opus_bridge.py`: `build_rx_argv`/`build_tx_argv` exact strings; RxBridge with a fake socket
  emits `on_opus` per datagram; TxBridge `feed_opus` sends `wrap_rtp` bytes; port allocation.
- `streams.py`: ref assignment stability, advertise payload shape vs `advertise.json` fixture.
- `engine.py`: subscribe→bridge start + frames flow; mic_state→TX + dead-man; tx_route.
- `ws_client.py`: against a **fake server speaking §5** (like `test_control_client`) — connect+
  advertise, source_subscribe starts producing binary frames, mic frame reaches TxBridge, format
  negotiation, `error` handling, reconnect backoff.
- `router_module.py`: descriptor shape, `tx_route` set/clear calls engine, broker inventory
  includes the virtual module; zero-virtual-module broker is unchanged.

Fixture-backed cross-session compatibility: B validates the same `tests/fixtures/audio/*` that
Session C will (Spec 0 §5.7).

Local real-opus evidence (dev): a scratch venv with `PyAV` generates `media_frame_slot0rx.bin`
and a `pytest.importorskip("av")`-gated test decodes it + Goertzel-checks the 1 kHz peak — so the
opus roundtrip is proven on the dev box even without the A-image. CI-visible suite never requires `av`.

`probe`: full E2E of the agent audio path against a fake §5 server + a fake router/bridge
substrate (RX tone → engine → server sees binary frames; server → engine → TxBridge receives
the injected Opus). No cross-repo reach-back.

## 13. Out of scope / follow-ups

- Server, Channels, web (Sessions C/D).
- **Real-HW audio-boundary green:** the bench (`192.168.88.211`) runs the pre-audio image;
  needs the merged A-image flashed/OTA'd first. Tracked as an explicit follow-up in the handback —
  the PR ships sim-green + unit/selftest-logic green; real-HW is a separate gate.
- Station-local idle services, occupancy signal, cross-band links (Spec 0 §11) — seams left, not built.
- QUIC datagram leg (Spec 0 Phase 2 / Session F).
- No linux-image companion PR required (minimal path uses only A-image tools).
