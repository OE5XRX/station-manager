# Audio Subsystem — Concept & Wire Contract (Spec 0)

**Status:** Draft for review
**Date:** 2026-09-03
**Scope repos:** `station-manager` (server, `station_agent`, web), `linux-image` (Yocto), `FW-RemoteStation` (udev/audio naming, minimal)

This is the reference document ("Spec 0") for the OE5XRX remote-station audio
subsystem. Every build session (A–F) references it. It defines the architecture,
the node/stream model, and the **normative wire contract** so that the agent side
and the server side can be built and tested independently yet stay compatible.

---

## 1. Purpose & Scope

Give an operator the ability to **hear and speak through a remote radio station over
the web**, and let additional users **listen**. FM (SA818, half-duplex, PTT) is the
MVP and the only audio hardware that exists today. The design is built **dynamically
from day one** for SSB, full-duplex, cross-band satellite operation, and multiple
audio modules per station — later hardware plugs in as a new driver, **no protocol
rebuild**.

**In scope (MVP):** FM RX heard in browser, browser mic → FM TX (PTT-gated),
end-to-end, over an unstable link (HAMNET / LTE/5G).
**Architected-for (not built now):** SSB/wideband, full-duplex, satellite cross-band,
N audio modules, per-user free source/sink choice.

---

## 2. Locked Decisions (with one-line rationale)

1. **Transport:** Opus over WebSocket, relayed through Django Channels.
   *Reuses existing WS/auth/lock infra; NAT-friendly outbound; easy to decompose.*
2. **Loss tolerance is mandatory, not optional:** Opus **in-band FEC + PLC** +
   **adaptive jitter buffer** on every stream. *Station link is lossy by default.*
3. **Transport behind an interface;** QUIC-datagram station↔server leg is the
   **designed Phase 2**. *Swap the lossy leg later without a rebuild.*
4. **Per-module streams, not pre-mixed buses.** Each audio module exposes its RX as
   a **source stream** and its TX as a **sink**; the operator mic is a **source**.
   *Enables free per-user selection.*
5. **Personal mixing lives in the browser (WebAudio).** The **server is a dumb Opus
   relay + fan-out**; it does **no DSP**. *Free per-user choice without server CPU or
   a media process next to Django.*
6. **RX/listen = browser-local mix. TX-routing = station-side, lock-gated.** *You can
   mix what you hear locally; the mic must physically enter one module's modulator via
   the station.*
7. **Station-side engine = PipeWire + WirePlumber (system-wide).** Handles I/O,
   resampling, clock domains, hotplug, and station-local routing (e.g. cross-band
   repeat). *Purpose-built; avoids a hand-rolled mixer.*
8. **Router is a virtual module on the existing control-plane.** Stream enumeration
   and TX-route commands are descriptor-driven like every other capability.
   *One control channel, dynamic, no FM special-casing.*
9. **Lock / PTT / dead-man reused unchanged.** Only the lock holder owns the mic +
   TX route; PTT dead-man tears TX down safely on link loss.
10. **Auth reused:** Ed25519 (agent audio-WS), session/OIDC + `can_use_station`
    (browser). Listeners need no lock.
11. **No DSGVO/privacy layer.** Amateur-radio transmissions are public by definition.

---

## 3. Architecture

```
 Browser (each user, own mix)        Django (dumb relay)            CM4: station_agent + PipeWire
 ┌───────────────────────────┐      ┌──────────────────┐          ┌───────────────────────────────┐
 │ WebAudio mix: subscribe    │      │ Fan-out per      │          │ PipeWire: I/O, resample, clock,│
 │  sources, gain/mute each   │◄════►│  (station,stream)│◄════════►│  hotplug, station-local routing│
 │ pick TX target + PTT + mic │ Opus │ Lock/PTT/auth    │  Opus    │ per module: RX-tap→ TX-inject← │
 │ (optional local sidetone)  │  /WS │ gate · NO DSP    │  /WS     │ Opus bridge (FEC) per stream   │
 └───────────────────────────┘      └──────────────────┘          │ Audio-WS client (Ed25519)      │
   /ws/audio/<station>/                /ws/agent/audio/<station>/  └───────────────────────────────┘
   Control-plane (existing): audio-router descriptor (dynamic streams) · tx_route cmd · PTT+dead-man
```

### Component responsibilities

- **CM4 / PipeWire:** system-wide PipeWire+WirePlumber. Each audio module's UAC2 ALSA
  device becomes a PipeWire node (capture=RX source, playback=TX sink). Resamples each
  module's native rate to the negotiated stream rate, handles clock drift and hotplug.
  Performs **station-local** routing (cross-band repeat = a link RX(A)→TX(B)). Maps
  `slot N → PipeWire node` via a stable `/dev/oe5xrx/slotN/audio` identifier — the
  module's UAC2 device on real hardware, an `snd-aloop` device under QEMU/CI (§8),
  indistinguishable to the agent.
- **station_agent:** control-plane translator (matrix/route commands ↔ PipeWire graph)
  **and** the audio bridge (per-stream Opus encode/decode via GStreamer) **and** the
  audio-WS client. Injects the operator-mic stream into the currently selected module
  TX. Demand-gated: only encodes/ships a source that has ≥1 subscriber.
- **Server (Django Channels):** authenticates both ends, **relays + fans out** opaque
  Opus frames per `(station, stream, direction)`, enforces lock/PTT gating on the
  uplink. No decode, no mix, no encode.
- **Browser:** subscribes to the source streams it wants, decodes (WebCodecs Opus),
  **mixes locally** (WebAudio gain/mute per source), captures mic (getUserMedia),
  encodes, sends uplink when keyed. Chooses TX-target module via the control-plane.

### The key asymmetry

- **RX / listening:** browser-local mix. Each user freely composes sources + levels.
  Default behavior comes from **browser-side subscription presets**, not station buses.
- **TX / routing:** station-side. "Which module do I transmit on" is a **`tx_route`
  command on the control-plane** (lock-gated); the agent injects the mic into that
  module's TX in PipeWire. TX is single-target.

### Buses dissolved

There are no station-side `operator_ear` / `monitor` buses. Instead:
- Each module RX = a selectable **source stream**.
- The operator mic = a selectable **source stream** (so a listener who subscribes to it
  hears the operator; during FM-TX the module RX is silent, so they naturally hear the
  operator).
- **Default presets (browser):**
  - *FM preset:* subscribe module-RX **and** operator-mic → hear the whole QSO from the
    station's perspective, no dead air during TX.
  - *Satellite/full-duplex preset:* subscribe module-RX (downlink) **only, not the mic**
    — the uplink returns via the downlink; mixing the local mic would double the audio.
  - *Operator sidetone:* browser-local monitor of own mic, optional, zero latency.

---

## 4. Node / Stream Model

A **stream** is identified by `(station_id, stream_id)` where `stream_id` is stable
for the lifetime of a module/direction on that station.

- **Source streams** (station → browser): `module RX`, `operator mic`, later `recorder
  tap`, `tone gen`. Direction = `rx` (from the browser's perspective: incoming to ear).
- **Sink** (browser → station): `operator mic` uplink lands on the server, is fanned
  out to subscribers as a source **and** injected into the current TX-route module.
- **Device mapping (real ⇄ sim identical):** the agent maps `slot N → PipeWire node`
  via a stable `/dev/oe5xrx/slotN/audio` identifier (udev), mirroring `slotN/control`.
  Real HW: the module's UAC2 ALSA device. QEMU/CI: an `snd-aloop` loopback fed by the
  sim-harness (§8). Indistinguishable to the agent.
- **Formats are per-stream and negotiated** (sample rate, channels) from the module
  descriptor — never hardcoded. FM = 8 kHz mono today; a future SSB/SDR module may be
  48 kHz.

---

## 5. Wire Contract (NORMATIVE)

Two new WebSocket endpoints, mirroring the existing control/terminal pattern. Router
**state** (available streams, tx_route) lives on the **existing control-plane**; the
audio-WS carries **media + lightweight per-connection signaling** only.

### 5.1 Endpoints & auth

- **Agent ↔ Server:** `wss://<host>/ws/agent/audio/<station_id>/`
  Auth: Ed25519 query-param signature (same scheme as agent control/terminal WS).
- **Browser ↔ Server:** `wss://<host>/ws/audio/<station_id>/`
  Auth: Django session/OIDC; `can_use_station(station)` required to connect (listen).
  Uplink (mic) additionally requires holding the station `ControlLock`.

### 5.2 Frame types

Text (JSON) frames = signaling. Binary frames = media. A connection multiplexes both.

**JSON envelope:** `{ "v": 1, "type": "<t>", ... }` (same versioning convention as the
control protocol; `v` is the audio-protocol version, independent of Opus/format).

#### Agent → Server (JSON)
- `advertise` — on connect and on hotplug: the source streams this station offers.
  ```json
  { "v":1, "type":"advertise", "streams":[
    { "stream_id":"slot0.rx", "slot":0, "module":"fm", "direction":"rx",
      "format":{"rate":8000,"channels":1}, "codec":"opus" },
    { "stream_id":"op.mic", "slot":null, "module":"operator", "direction":"rx",
      "format":{"rate":16000,"channels":1}, "codec":"opus" }
  ]}
  ```
- `stream_state` — a source started/stopped/errored: `{ "v":1,"type":"stream_state",
  "stream_id":"slot0.rx","state":"live|idle|error","detail":"..." }`

**Producer semantics:** `advertise` only declares *availability*. `slot<N>.rx` media is
produced by the agent; `op.mic` media is produced by the **operator's browser** (via
`mic_open`, gated by lock+PTT) and the server fans it out to the agent (TX injection) and
any subscribers.

#### Server → Agent (JSON)
- `source_subscribe` / `source_unsubscribe` — demand gating: start/stop producing a
  source (server tracks browser subscriber count).
  `{ "v":1,"type":"source_subscribe","stream_id":"slot0.rx" }`
- `mic_state` — uplink is authorized-and-keyed / stopped (mirrors lock+PTT), so the
  agent knows to expect/inject mic media:
  `{ "v":1,"type":"mic_state","active":true,"tx_slot":0,"tx_module":"fm" }`

#### Browser → Server (JSON)
- `hello` — client capabilities: `{ "v":1,"type":"hello","codecs":["opus"],
  "webcodecs":true }`
- `subscribe` / `unsubscribe` — `{ "v":1,"type":"subscribe","stream_ids":["slot0.rx","op.mic"] }`
- `mic_open` / `mic_close` — declare uplink format (server enforces lock+PTT before
  relaying/injecting): `{ "v":1,"type":"mic_open","format":{"rate":16000,"channels":1},
  "codec":"opus" }`

#### Server → Browser (JSON)
- `streams` — current available source streams (filtered/relayed from `advertise`).
- `stream_state` — relayed source lifecycle.
- `error` — `{ "v":1,"type":"error","code":"not_locked|not_authorized|
  unknown_stream|format_unsupported","detail":"..." }`

### 5.3 Media frames (binary, both hops)

Little-endian header, then the raw Opus packet:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | `magic` | `0xA5` |
| 1 | 1 | `ver` | audio-frame version = `1` |
| 2 | 2 | `stream_ref` | numeric handle for a `stream_id` (mapped at subscribe/advertise) |
| 4 | 2 | `seq` | wraps at 2^16; per-stream |
| 6 | 4 | `ts` | RTP-style timestamp in samples at the stream rate |
| 10 | 1 | `flags` | bit0=FEC-present, bit1=DTX/comfort, bit2=marker(keyframe/talk-onset) |
| 11 | 1 | `reserved` | 0 |
| 12 | … | `payload` | one Opus packet (20 ms frame) |

`stream_ref` avoids sending the string id per frame; the mapping `stream_ref ↔
stream_id` is established in `advertise`/`streams` and `subscribe` acks.

### 5.4 Opus profile (normative defaults)

- Frame size **20 ms**, **VBR**, application = VOIP, **in-band FEC on**, **PLC on** at
  the receiver, DTX allowed. Bitrate adaptive; NB/WB per negotiated rate.
- Encode rate = negotiated stream rate (PipeWire resamples the module's native rate to
  it). MVP FM: 8 kHz NB or 16 kHz WB (decided in Session B spec).
- Receivers MUST implement PLC (conceal) on gaps and MUST use FEC data when `flags`
  bit0 is set.

### 5.5 Gating rules (server-enforced)

- **Downlink (source → browser):** allowed to any connection that passed
  `can_use_station`. Fan-out shares one agent-produced stream to N subscribers.
- **Uplink (browser mic):** relayed/injected **only while** the sender holds the
  `ControlLock` **and** PTT is active for a gated module. Otherwise dropped + `error`.
- **Demand gating:** the server subscribes a source at the agent only while ≥1 browser
  subscriber exists; unsubscribes at zero.

### 5.6 Control-plane additions (existing `/ws/control` + registry)

The audio router appears as a **virtual module** `audio-router` (synthetic slot) in the
control descriptor, with:
- Dynamic enumeration of available audio streams (mirrors `advertise`).
- Capability `tx_route` (`op:set`, value = `{slot,module}` or `null`) — sets which
  module the operator mic transmits into. **Lock-gated.**
- Future: `link_add`/`link_remove`/`gain` for station-local routes (cross-band repeat).

### 5.7 Contract fixtures (for independent testing)

Golden fixtures live at `station-manager/tests/fixtures/audio/` and are consumed by
**both** the agent tests (Session B) and the server tests (Session C):
- `advertise.json`, `subscribe.json`, `mic_open.json`, `error_not_locked.json`
- `media_frame_slot0rx.bin` — a header + one real Opus packet (a 1 kHz tone), used to
  assert header parse + round-trip decode (FFT peak at 1 kHz).

---

## 6. Phasing

1. **FM vertical slice** — one RX source end-to-end (hear it) + mic→FM-TX (PTT). Proves
   the whole pipeline and the contract.
2. **Multi-stream + mixer UI** — N sources, browser mix, presets, tx_route selection.
3. **Yocto hardening** — image services, udev naming, on-target HIL.
4. **Phase 2 transport** — QUIC-datagram station↔server leg behind the transport
   interface.

(Sessions A–E deliver phases 1–3 interleaved; F is phase 4, later.)

---

## 7. Testing Strategy & "Okay" Gates

TDD is the default. `probe` runs **full E2E**. Every session ends with concrete
verification evidence (verification-before-completion) that the reviewer checks against
this spec. Each session self-runs the two-stage review (atlas spec-compliance → audit
quality) before hand-back.

| Session | Test approach | "Okay" evidence |
|---|---|---|
| A Yocto/Image | image build; boot smoke (QEMU x64 + rpi64); service-up | `wpctl status` shows module node; udev per-slot symlink; opus/gstreamer present |
| B Agent | unit (RouterBackend w/ mocked pw-link, bridge framing, audio-WS client vs fake server per contract); `selftest` | selftest: loopback tone → Opus → decode → FFT peak; unit suite green |
| C Server | Channels consumer tests: auth both ends, fan-out to 2 browsers, lock/PTT gate, reconnect (opaque frames) | consumer suite green (gate + fan-out especially) |
| D Web | JS unit (subscribe/preset logic, mixer gain/mute, PTT-audio coupling, WebCodecs wrapper mocked) | JS suite green; live: operator hears test RX; PTT routes mic |
| E E2E/HIL | probe full FM slice; mic+PTT→module TX keyed; multi-stream; netem loss for FEC/PLC/jitter | FM operable over web end-to-end |

**Cross-session compatibility** is guaranteed by the §5.7 fixtures: B and C both
validate against them, so they interoperate without having been built together.

**No-hardware CI:** audio in QEMU has no SA818/UAC2 — it comes from the sim-harness
`snd-aloop` substrate (§8). The same known-tone (1 kHz → FFT peak) assertion runs in sim
and on the CM4 bench.

---

## 8. Test & Simulation Substrate (QEMU / CI)

QEMU x86-64 is the CI target and has **no SA818 and no UAC2 audio device**. Following
the existing module-simulation pattern (`docs/superpowers/specs/2026-07-04-module-simulation-layer-design.md`,
Issue #30), audio in CI is emulated **inside the guest**, not via QEMU devices:

- **Control today:** a pinned `native_sim` Zephyr binary + `sa818-sim.py` on PTYs; the
  sim-harness symlinks `/dev/oe5xrx/slot1/control`. The agent behaves identically to real HW.
- **Audio (new, this design):** the sim-harness loads **`snd-aloop`** (ALSA kernel
  loopback) and exposes it as `/dev/oe5xrx/slotN/audio`, mirroring the serial bridge. A
  small **tone shim** (a 1 kHz generator — the audio analog of `sa818-sim.py`) feeds the
  loopback so the agent's RX tap reads a known signal. The tone runs at **8 kHz mono
  S16_LE** to mirror the real UAC2 module (§12) and exercise the real 8 k→48 k resample
  path. The substrate is **bidirectional**: the same aloop card's reverse cable exposes a
  TX sink `oe5xrx.slotN.tx` (what PipeWire plays there appears on a capture the TX
  self-check records → a distinct-frequency Goertzel probe), so Session B can test mic→TX
  in sim with no cross-repo reach-back. PipeWire sees the aloop as normal nodes; the
  `audio-router` descriptor advertises them like any module.
- **Same-kernel constraint is fine:** `snd-aloop` only bridges within the guest kernel —
  but the agent immediately Opus-encodes and ships over WS, so audio leaves QEMU as
  **network traffic**, not host audio. No QEMU audio backend is ever needed.
- **Known-tone assertion threads every layer:** agent selftest (FFT peak on the tap),
  server relay (opaque frames), headless browser (WebCodecs decode → OfflineAudioContext
  FFT), E2E. On the CM4 bench the real SA818 (or an injected RF tone) replaces the shim —
  **same tests, two targets.**

Delivered in **Session A** (sim-harness `snd-aloop` extension + tone shim + `slotN/audio`
udev), consumed by B/C/D/E.

---

## 9. Session Decomposition

| # | Session | Repo(s) | Core deliverable |
|---|---|---|---|
| 0 | Concept + contract (this doc) | station-manager/docs | reference for all |
| A | Yocto/image audio foundation | linux-image (+FW/udev) | PipeWire/WirePlumber/GStreamer/libopus in image; system services; udev `slotN/audio` naming; **sim-harness `snd-aloop` substrate — bidirectional RX+TX, 8 kHz mono, tone shim** (§8) |
| B | station_agent audio | station-manager/station_agent | PipeWire backend, module tap/inject, Opus bridge, audio-WS client, audio-router capability, mic→TX + PTT |
| C | server audio relay | station-manager (server) | Channels consumers (agent+browser), fan-out, lock/PTT gate, descriptor in control registry |
| D | web audio client + mixer UI | station-manager (web) | WebCodecs Opus, WebAudio mix, subscribe/presets, operator quickview + matrix view |
| E | E2E integration + HIL | station-manager | probe full-E2E FM slice then multi-stream |
| F | *(Phase 2)* QUIC datagram station leg | station-manager | transport swap behind interface |

Build order: **0 → A → (B ∥ C) → D → E**. B and C parallelize against the contract.

---

## 10. Open Implementation Decisions (deferred to component specs, with recommendation)

- **Bridge encoder:** GStreamer (`pipewiresrc ! opusenc inband-fec=true`) *(recommended,
  robust FEC/PLC)* vs. in-agent `opuslib`. Behind a bridge interface either way.
- **Graph control:** WirePlumber + `pw-link`/`wpctl` *(recommended for MVP)* vs.
  libpipewire bindings. Behind a `RouterBackend` interface.
- **MVP FM encode rate:** 8 kHz NB vs 16 kHz WB (Session B).
- **WebCodecs fallback:** browsers without WebCodecs Opus → WASM decoder fallback?
  (Session D; target browsers TBD.)
- **Tone-shim home:** co-versioned FW-RemoteStation release asset (like `sa818-sim.py`)
  vs. self-contained in the linux-image sim-harness (Session A).
- **`slotN/audio` udev matching:** how the UAC2 ALSA card is matched to a USB port path
  (analogous to the `slotN/control` tty rule) for a stable per-slot audio device name.

---

## 11. Design-for: Station-local audio services (NOT built now)

When a station is idle, secondary services may run **on the CM4** — e.g. APRS decode on
2 m, SSTV/WSPR/FT8 decode, a recorder, an ident/beacon, or a parrot repeater. **Not built
in Sessions A–E**, but the architecture must not preclude it.

- **A local service is just another PipeWire node.** Because routing lives on the CM4, a
  decoder is a **local sink** (`slot0.rx → direwolf`), a beacon a **local source**
  (`beacon → slotN.tx`). Audio reaches it via a PipeWire link — **no network**. Decode
  locally, send only the *decoded data* upstream (reuse the control-plane/telemetry path);
  never ship raw RX up the constrained link just to decode it.
- **Strict mutual exclusion with the operator.** Secondary services run **only while the
  station is idle** (no operator logged in / no active operator session). The moment an
  operator takes the station, the agent **stops all secondary services first**; they resume
  only after the operator leaves. **Operator always wins — no coexistence, by design**, to
  avoid conflicts. (Technically a pure-RX decoder *could* fan out during operation, but the
  policy forbids it for simplicity and safety.)
- **Lifecycle & idle policy:** the **agent** owns start/stop of these services (systemd
  units), gated on station occupancy, and guarantees a clean stop **before** handing the
  station to an operator.

**Non-preclusion hooks the MVP must honor:** (1) the agent must expose a single **occupancy
signal** ("is an operator present on this station?") that gates service start/stop — don't
scatter this so it can't be centralized later; (2) the agent's control/lock model must not
hardcode "only a browser lock-holder can request TX" — leave room for a non-human TX
requester that runs only while idle. The `RouterBackend` may still allow N local sink
consumers (several idle-time services at once), just **never concurrent with an operator**.

---

## 12. Appendix — Real-HW bench reference (RPi4, pre-audio image)

Authoritative data pulled from the bench FM module, to de-risk Session B / real bring-up:

- **Module:** OE5XRX FM Transceiver Board, USB `2fe3:0012`, serial
  `2031394D3646500E004B004F`, ALSA card id `Board`. Composite iface set
  `:020200:0a0000:fe0101:010120:010220:` = CDC-ACM + DFU + UAC2, driver `snd-usb-audio`.
- **Topology:** FE1.1s hub `1a40:0101` at `1-1`; module on **hub port `1-1.3` → slot3**
  (`/dev/oe5xrx/slot3/control → ttyACM0`).
- **UAC2 format (authoritative):** **8000 Hz, 1 ch mono (FC), S16_LE**, both directions —
  confirms the FM 8 kHz mono assumption. PCM: `pcmC1D0c` = RX (capture), `pcmC1D0p` = TX
  (playback). Playback EP 0x02 OUT ASYNC + sync EP 0x83; Capture EP 0x84 IN.
- **Stable identifiers:**
  - by-path `/dev/snd/by-path/platform-fe9c0000.xhci-usb-0:1.3:1.3` — encodes the USB
    port; PipeWire `api.alsa.path` / `device.bus-path` derive from this.
  - by-id `/dev/snd/by-id/usb-OE5XRX_FM_Transceiver_Board_<serial>-03` — encodes
    vendor_model_**serial**; PipeWire **`node.name`** derives from this — **no port path**.

**Finding 2 resolution (data-backed) — carried into Session B:**
- The Session-A WirePlumber real-HW rule matching `node.name ~ "usb.*1\.1"` **cannot match**:
  the real node.name carries the serial, not the port (and this module is on port `.3`).
  Confirmed broken on real HW; inert in sim, so CI-green stays valid.
- **Fix:** do **not** hardcode per-port node names in WirePlumber. Have the **agent map
  slot → PipeWire node via the `OE5XRX_SLOT` udev tag** (already set by the Session-A udev
  rule per `KERNELS==1-1.N`, verified against the real DEVPATH `…/1-1/1-1.3/…`):
  udev(sound dev, `OE5XRX_SLOT=N`) → ALSA card index → PipeWire node via `api.alsa.card`.
  One mechanism for control **and** audio (the agent already does this for `slotN/control`).
  The sim tags the aloop card `OE5XRX_SLOT=1`, so sim and real resolve identically.
  Session A drops its (proven non-matching) real-HW WirePlumber rename rule; only the sim
  rename remains.
- **Slot-parametric everywhere:** the bench module is **slot3**, the sim is **slot1** —
  nothing outside the sim substrate may hardcode a slot number.