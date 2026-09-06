# Session E Handback — Audio E2E / HIL Integration

**Date:** 2026-09-06
**Scope:** Wire + test the audio subsystem end-to-end (Sessions A–D already in `main`).
E does **not** rewrite any A–D contract/component; it verifies them and fixes real
integration bugs found in the process.
**Status:** Tier 1 delivered (agent funks the sim tone end-to-end through the real
PipeWire in the image, FFT-proven in QEMU). Tier 2 scoped + planned, not built.
**NOT merged.** Two PRs, PR-ready for human review.

---

## 1. Branches & PRs

| Repo | Branch | PR | Contents |
|---|---|---|---|
| station-manager | `feat/audio-e2e-selftest-fix` | **#124** | Repairs the never-run `selftest audio` **TX** path (3 coupled defects) + `_capture` timeout-salvage BLOCKER + TX-spawn guard; `RouterBackend.alsa_card_for_slot()`; this handback. |
| linux-image | `feat/audio-agent-e2e` | **#86** | New QEMU okay-gate `test_audio_agent_e2e.py` (`-m qemu`) + `tests/audio/agent_audio_selfcheck.sh` running `python -m station_agent selftest audio --slot 1` in the guest; ci.yml shellcheck; **temporary** station-agent test-pin to #124. |

**Cross-repo pin (must resolve before merge):** #86's `station-agent_0.1.0.bb` pins
`branch=feat/audio-e2e-selftest-fix` + #124's SRCREV so CI can build+prove the fix
green **before** #124 merges. Sequencing: merge #124 → `scripts/pin-station-agent.sh
<squashed-main-sha>` + revert `branch=main` in #86 → merge #86.

---

## 2. Tier 1 (PFLICHT) — agent-side A+B end-to-end in QEMU

**What it proves.** The merged qemux86-64 image boots PipeWire + WirePlumber + the
Session-A `snd-aloop` sim substrate (1 kHz shim on `oe5xrx.slot1`, TX sink
`oe5xrx.slot1.tx`) + the audio-capable `station-agent`. The new gate logs in over the
serial console and runs **the agent's own** self-check in the guest:

```
python -m station_agent selftest audio --slot 1 --duration 2 --tx-freq 1500 --rate 8000
```

- **RX:** the agent taps `oe5xrx.slot1` through its **RouterBackend + Opus bridge**
  (gst `pipewiresrc ! … ! opusenc ! opusdec ! … ! fdsink`) and asserts the 1 kHz shim
  survives the encode→decode roundtrip via its pure-Python Goertzel probe.
- **TX:** the agent injects a distinct **1500 Hz** tone (full Opus roundtrip) into
  `oe5xrx.slot1.tx` and verifies it on the aloop **reverse-cable tap** (raw dev0
  capture, Spec 0 §8) — the exact end A's reference `audio_selfcheck.sh` records.

This is a layer above Session A's row-A gate (`audio_selfcheck.sh`, which checks the
raw PipeWire/ALSA substrate): E drives the **agent's** audio engine, proving **A+B**
together on the agent side against the real image. The agent's Goertzel verdict **and**
the FFT dominance ratio it logs `P(target)/P(runner-up)` ARE the proof — the guest
ships no WAVs and the host does no FFT. The test additionally re-parses both ratios
host-side and requires `>4×`, so a future logging change can't silently weaken the gate.

**Contention (Spec-E note).** `station-agent.service` runs in the image. The self-check
`systemctl stop station-agent` before running the selftest so nothing can contend for
the TX sink (the agent doesn't touch audio with the seeded config, but stopping it makes
the field deterministic). RX fan-out would also have worked (PipeWire allows N source
readers); the TX sink is the only exclusive resource, hence the stop.

### FFT-peak evidence (green QEMU run)

> **⟨FILLED IN ON GREEN CI — see PR #86 run below⟩**
>
> ```
> <paste the `----- station_agent selftest audio guest output -----` block here:
>  selftest audio: RX OK — 1000 Hz recovered through Opus (P(1000)/P(runner-up)=…x)
>  selftest audio: TX OK — 1500 Hz on reverse tap (P(1500)/P(runner-up)=…x)
>  selftest audio: PASS (slot 1) …
>  AGENT-AUDIO-E2E result=PASS slot=1>
> ```
> Run: <link to the green `Boot & OTA (qemux86-64)` job on PR #86>

---

## 3. Integration bugs found & fixed (in B's `selftest audio`)

Running `selftest audio` against A's **real** substrate for the first time surfaced
that B's TX self-check **had never executed end-to-end** — B's unit tests injected the
capture/play seams and only asserted argv contents (`test_audio_selftest.py` even
codified `opusdec not in j`). Four defects, all fixed minimally (RX path, engine,
bridge, WS client, and the §5 wire contract untouched):

1. **`build_tx_play_argv`: `opusenc ! audioconvert` never links.** opusenc emits
   `audio/x-opus`; audioconvert wants raw PCM → the pipeline can't negotiate caps.
   Added the missing **`opusdec`** → a full Opus roundtrip, mirroring the production
   mic path (browser `opusenc` → agent `opusdec` → TX sink).
2. **Wrong loopback end.** The check tapped `rx_node` (aloop **dev1 capture** = cable A
   = the continuous 1 kHz RX shim), not the reverse cable. The injected 1500 Hz lands
   on the aloop **dev0 capture** (cable B), a raw ALSA end WirePlumber leaves un-owned
   (Spec 0 §8). Now taps `hw:<card>,0,0` via `arecord`. Card resolved via the
   `OE5XRX_SLOT` udev tag (§12 Finding 2 — slot-parametric, no hardcoded aloop id).
3. **Play/capture didn't overlap.** `run_audio` played the tone to completion **then**
   captured — a real-time loopback carries nothing unless the source is live during
   capture. Now spawns the tone in the background, waits for the aloop TX playback to
   reach `RUNNING`, captures concurrently, then stops it.
4. **`_capture` discarded PCM on timeout (BLOCKER, found in review).** The RX pipeline
   has no `num-buffers`, so `gst-launch` never self-terminates and `subprocess.run`
   **always** raises `TimeoutExpired` — the old code returned `b""`, so **both RX and
   TX** captured nothing on real gst (final proof the selftest never ran end-to-end).
   Now salvages `exc.stdout` (the PCM captured up to the kill).

Also hardened: a raising TX `spawn` (missing gst-launch/plugin) now returns a clean
`rc=1` instead of a traceback; added public `RouterBackend.alsa_card_for_slot()`.

**Honesty rule.** The TX check is a **sim-loopback capability** by design — the reverse
cable exists only in `snd-aloop`. On the real UAC2 FM module the TX playback EP does not
loop to the RX capture EP (TX leaves as RF), so real-HW TX verification stays an RF/bench
follow-up. The code fails **closed** (no reverse tap → FAIL, not false-pass).

---

## 4. Two-stage review + verification

- **atlas (Spec 0 §5/§7/§8 compliance):** **PASS-with-conditions.** Verified the TX
  topology match against A's §8 (dev1 playback → dev0 capture reverse cable), the
  slot→card resolution vs §12 Finding 2, that the Tier-1 gate genuinely proves row E
  (RX Opus roundtrip + TX reverse tap, no false-pass), and that the §5 wire contract is
  untouched. Conditions = the temporary cross-repo pin (§1) + confirm `alsa-utils` in
  the image (confirmed: `oe5xrx-audio-system_1.0.bb` installs `alsa-utils`).
- **audit (quality/concurrency/CI-robustness):** **APPROVE WITH CHANGES.** Found the
  `_capture` timeout BLOCKER (fixed, §3.4), the TX-spawn traceback MAJOR (fixed), and a
  ruff line-length + brittle-assert MINOR (fixed). Confirmed the `try/finally` process
  lifecycle, fail-closed logic, and POSIX-sh portability are sound.
- **Unit verification (station-manager, local):** `pytest tests/test_audio_*.py` →
  **119 passed, 1 skipped** (av-gated Opus decode); `ruff check` + `ruff format --check`
  clean. New regression tests lock every fixed defect (incl. the exact "tapped cable A"
  bug and the timeout-salvage).
- **E2E verification:** the green QEMU run on PR #86 (§2 evidence) — the real proof.

---

## 5. Tier 2 (BEST-EFFORT) — full-stack loop: scoped + planned, NOT built

A full cross-process loop — **Agent(QEMU) → Django/Channels(C) → headless
Chromium/WebCodecs(D)** with netem loss — is **3–4 days** of orchestration work (no
architectural blockers, but heavy: QEMU + Daphne + Chromium synchronization, and a
headless WebCodecs→PCM→FFT capture path that doesn't exist yet). Out of scope for one
autonomous session per the Session-E brief ("nicht forcieren"). Concrete plan for a
follow-up session, grounded in the C/D handbacks + code:

**Endpoints/auth (verified).** Agent: `wss://…/ws/agent/audio/<station>/` (Ed25519
query-sig, `"{ts}:{sha256('')}"`, 60 s skew, current+next `DeviceKey`). Browser:
`wss://…/ws/audio/<station>/` (session/OIDC + `can_use_station`; uplink needs
`ControlLock`+PTT). Discriminated in `config/asgi.py`.

**Server bring-up (easy).** `DJANGO_SETTINGS_MODULE=config.settings.test` →
SQLite + **InMemoryChannelLayer** (no Redis). Either `WebsocketCommunicator`
(in-process, no server) or `daphne config.asgi:application` for a real socket. Existing
seams: `tests/test_audio_consumer.py` (30+), `tests/test_audio_server_relay_e2e.py` (6),
`audio_agent_auth` conftest fixture (monkeypatch `_verify_agent`).

**Agent side.** Enable `config.audio_enabled=true` + `audio_*` knobs (config.py); point
`AudioClient` at the test server URL; seed a `DeviceKey` matching the agent's Ed25519.

**Browser side (the hard part).** Control page `/<station_pk>/control/`; needs
Chromium ≥106 (WebCodecs). No jest/vitest/playwright harness exists — pure-logic JS is
Node-tested (`tests/js/audio-logic.test.mjs`). New scaffolding: Playwright/Puppeteer +
route decoded RX through `OfflineAudioContext`/a recorder → PCM → reuse the Goertzel
1 kHz assertion. Mic path: Puppeteer fake-getUserMedia tone → PTT+lock → uplink →
server → agent → `oe5xrx.slot1.tx` → sim reverse-tap 1500 Hz check (reuse Tier-1's tap).

**netem.** None today. Add `tc qdisc netem loss/delay` on the loopback/veth between
agent and server; assert PLC/FEC recovery (Opus in-band FEC is decoder-internal).

**Suggested staging:** (1) in-process `WebsocketCommunicator` agent-frame → browser-frame
relay with the golden Opus fixture (cheap, no browser); (2) add real Chromium RX-decode
FFT; (3) add mic uplink → sim TX tap; (4) add netem. Each stage is independently green.

---

## 6. OPEN — Real-HW audio-boundary (tracked follow-up, NOT this session)

Per the audio-boundary honesty rule (analog to the serial rule in
`station-manager/CLAUDE.md`), **sim-green is necessary, not sufficient.** The boundary is
not truly green until it passes on real CM4/bench HW:

- Bench `root@192.168.88.211` (RPi4, real FM module on **slot3**, UAC2 8 kHz mono).
- Flash/OTA the merged A+B image onto the bench first, then
  `python -m station_agent selftest audio --slot 3`.
- **RX** (1 kHz through the agent Opus roundtrip) is the substrate-agnostic proof and
  should pass on real HW. **TX** via the aloop reverse cable does **not** exist on real
  HW (TX leaves as RF) — real-HW TX verification needs RF loopback (a second radio or an
  injected tone) and is a separate bench task, not the electrical self-check.
- Nothing outside the sim substrate hardcodes a slot (sim=slot1, bench=slot3 are params).

---

## 7. Not done (out of scope, by design)

Tier 2 full loop (§5 — planned), netem/loss suite, real-HW bench (§6), station-local
idle services / occupancy signal / cross-band links (Spec 0 §11), QUIC datagram leg
(Phase 2 / Session F).
