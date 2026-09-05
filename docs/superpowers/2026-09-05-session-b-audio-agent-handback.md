# Session B Handback — station_agent Audio Pipeline

**Date:** 2026-09-05
**Branch:** `feat/station-agent-audio` (off `origin/main` @ 415d18b)
**Scope:** station_agent audio subsystem only (agent side). No server/Channels (C), no web (D).
**Status:** Sim-green + unit/E2E green, ruff clean, PR-ready. **NOT merged.** Real-HW audio-boundary is an explicit open follow-up.

---

## 1. What was built

New subpackage `station_agent/audio/` (all TDD, subprocess/socket/gst seams injected so unit
tests need no PipeWire/GStreamer/numpy/opus):

| Module | Role |
|---|---|
| `frame.py` | §5.3 media-frame pack/parse (LE 12-byte header + Opus payload) |
| `rtp.py` | minimal RTP wrap/strip for the gst-launch↔agent UDP boundary |
| `goertzel.py` | pure-Python single-bin power detector (selftest FFT, no numpy) |
| `router_backend.py` | slot→node via `OE5XRX_SLOT`→`api.alsa.card` (Finding 2); `pw-link`/`wpctl` graph ops |
| `opus_bridge.py` | gst-launch RX/TX pipelines (FEC/PLC/jitterbuffer) + RTP/UDP + port allocator |
| `streams.py` | `stream_id↔stream_ref` registry + advertise payload |
| `engine.py` | demand-gated RX bridges, mic→TX injection, mic dead-man + TOT, tx_route |
| `bridge_factory.py` | default bridge factory with UDP port management |
| `ws_client.py` | persistent Ed25519 audio-WS client (analog `control_client.py`), reconnect backoff |
| `router_module.py` | `audio-router` virtual control-plane module (§5.6): `tx_route` + `streams` |
| `selftest.py` | `python -m station_agent selftest audio` (Goertzel RX/TX tone probe) |

Wiring: `config.py` (audio_* knobs, off by default), `agent.py` (AudioClient thread +
audio-router registration on the control broker when audio+control enabled), `__main__.py`
(`selftest audio` subcommand), `broker.py` (guarded `virtual_modules` seam),
`control_client.py` (forwards `virtual_modules`).

## 2. Changed/added files

- **New:** `station_agent/audio/{__init__,frame,rtp,goertzel,router_backend,opus_bridge,streams,engine,bridge_factory,ws_client,router_module,selftest}.py`
- **Modified:** `station_agent/{config,broker,control_client,agent,__main__}.py`, `station_agent/config.example.yml`
- **Tests (new):** `tests/test_audio_{frame,rtp,goertzel,router_backend,opus_bridge,engine,ws_client,router_module,selftest,fixtures,e2e}.py`
- **Fixtures (new):** `tests/fixtures/audio/{advertise,subscribe,source_subscribe,mic_open,mic_state,error_not_locked}.json`, `media_frame_slot0rx.bin` (+ `gen_media_frame.py` reproducer)
- **Docs:** `docs/superpowers/specs/2026-09-05-audio-agent-component-design.md` (component spec), amendments to `docs/superpowers/specs/2026-09-03-audio-subsystem-design.md` (Spec 0), this handback.

## 3. §10 decisions (locked, with user confirmation)

| §10 item | Decision |
|---|---|
| Bridge encoder | **gst-launch subprocess** (no new image deps) |
| Graph control | **pw-cli/pw-dump/pw-link/wpctl subprocess** |
| MVP FM encode rate | **RX 8 kHz NB** (module native); op.mic 16 kHz WB |
| slot→node | **`OE5XRX_SLOT` udev → ALSA card → `api.alsa.card`** (Spec 0 §12 Finding 2), never node.name/port |
| discrete Opus packets from gst-launch | **RTP over UDP loopback** (`rtpopuspay`/`rtpopusdepay`) |
| selftest FFT | **pure-Python Goertzel** |
| real-HW gate | **sim-green now, real-HW as follow-up** |

**No new station_agent runtime deps.** `pyproject.toml`/`requirements.txt` unchanged.
**No linux-image companion PR needed** — the minimal path uses only tools already in the A-image.

## 4. Contract clarification (amended into Spec 0)

Spec 0 §5.3 said the `stream_ref↔stream_id` mapping is "established in advertise" but the §5.2
example carried no field for it. Resolved by making **`stream_ref` an explicit u16 field in each
`advertise` entry** (the agent produces media and owns ref assignment). Consumers MUST read it,
not infer from array index. Spec 0 §5.2/§5.3 and `advertise.json` updated; `media_frame_slot0rx.bin`
header `stream_ref` (0) matches `slot0.rx`'s advertised ref. Also amended: **`flags` bit0 (FEC) is
advisory** — the MVP relies on Opus in-band FEC and leaves bit0=0; consumers must not gate FEC on it.

## 5. Okay-gate evidence (test output)

```
ruff check station_agent/ tests/test_audio_*.py  → All checks passed!
pytest tests/test_audio_*.py                     → 71 passed, 1 skipped
  (skip = av-gated Opus decode; av not on the default interpreter)
full audio + broker/control/config regression    → 111 passed, 1 skipped
```

- **Contract fixtures (§5.7):** `media_frame_slot0rx.bin` = §5.3 header + a REAL 1 kHz Opus
  packet; verified via PyAV scratch venv that it parses and decodes to a dominant 1 kHz tone
  (P(1000)/P(2000) ≈ 3400×). All JSON fixtures validate + match §5.
- **E2E (`test_audio_e2e.py`):** real `AudioClient`+engine vs a fake §5 server — RX tone →
  §5.3 frame at server (header/ref/payload verified; av-gated 1 kHz decode); server mic_state
  + media frame → TX bridge `feed_opus` byte-exact. Passes green with av absent.
- **selftest logic** unit-tested (pipeline argv + Goertzel verdicts) with injected capture/play;
  the command itself runs on-target only.

## 6. Two-stage review + watchers (all findings addressed)

- **atlas (spec-compliance §5):** PASS with conditions — byte-level media contract exact.
  Fixed: out-of-set error codes for node-resolution (now `stream_state`); Spec 0 amendments
  (explicit stream_ref, bit0 advisory) landed; tx_route descriptor type note aligned.
- **audit (quality/concurrency):** no blockers; fixed 3 MAJOR — blocking subprocess/bind
  offloaded off the WS loop (`run_in_executor`); dead-man/TOT token-guard + detach-before-teardown
  (a fired timer could self-cancel its own offloaded stop); late RX reader-thread callback drop;
  port-leak on failed `start()`; thread-safe PortAllocator; ws_client TOCTOU + narrowed excepts.
- **guard (security):** fixed 1 **BLOCKER** — TX `udpsrc` bound `0.0.0.0`, allowing
  unauthenticated RTP injection into the **keyed transmitter** audio path; now `address=127.0.0.1`
  (loopback-only). Added absolute mic **TOT** (default 180 s) bounding a looping/injected uplink.
  Clarified the mic dead-man tears down TX *audio*, not the carrier (control-plane PTT dead-man
  is the authoritative carrier interlock — Session C).
- **code-simplifier:** minor dedupe in selftest.py; rest already clean.
- **probe:** wrote the E2E; found no production bugs.

## 7. Deviations / notes for Session C

- **advertise carries explicit `stream_ref`** (see §4) — build the ref map from advertise, not
  index. Validate against `tests/fixtures/audio/*` (§5.7) — B and C share them.
- **Error codes:** the agent only emits §5.5-set codes on the audio-WS. Node-resolution/lifecycle
  failures are reported via `stream_state {state:"error"}`, not `error` frames.
- **Auth:** the agent's Ed25519 signed string is `"{timestamp}:{sha256('')}"` (empty body),
  `station_id` in the query only — identical to the control/terminal scheme. The server MUST
  enforce a tight timestamp-skew window and bind the connection's station to the signing key
  (not the client-supplied `station_id`), and enforce lock+PTT gating on the uplink (§5.5).
- **audio-router** appears as a virtual control-plane module at synthetic slot 1000 (configurable)
  with caps `streams` (telemetry) + `tx_route` (action). `tx_route` is lock-gated *server-side*;
  the agent records the route and acks. The authoritative mic-injection target reaches the agent
  via `mic_state.tx_slot` on the audio-WS.

## 8. OPEN — Real-HW audio-boundary (follow-up, blocked on image)

**Per the audio-boundary honesty rule (analog to the serial rule in `station-manager/CLAUDE.md`),
this boundary is NOT truly green until it passes on real CM4/bench HW.** Sim-green is necessary,
not sufficient.

- Bench: `root@192.168.88.211` (RPi4, real FM module on **slot3**, UAC2 8 kHz mono).
- **Blocker:** the bench currently runs the PRE-audio image (no PipeWire). The merged A-image
  (linux-image PR #84) must be flashed/OTA'd onto the bench first.
- Then run: `python -m station_agent selftest audio --slot 3` (bench is slot3, not slot1) and
  confirm the RX 1 kHz + TX distinct-tone Goertzel probes pass on real hardware; ideally a full
  browser→agent→FM-TX keyed E2E once Sessions C/D land.
- Nothing outside the sim substrate hardcodes a slot number (sim=slot1, bench=slot3 are parameters).

## 9. Not done (out of scope, by design)

Server/Channels (C), web/mixer (D), station-local idle services + occupancy signal + cross-band
links (Spec 0 §11 — seams left, not built), QUIC datagram leg (Phase 2 / Session F). No
linux-image companion PR was required.
</content>
