# SSTV receive & transmit — Design

**Date:** 2026-11-24 · **Status:** draft for review · **Repos:** station-manager (server + `station_agent`), linux-image (SSTV decoder/encoder in the decoder unit), OE5XRX.github.io (privacy policy text)

Marks: **(user)** = decided by the user. **(call)** = designer's call, open to correction. Everything unmarked follows from existing architecture.

## 1. Intent

Users want to receive and send SSTV pictures through a remote station: ISS SSTV events (PD120 on 145.800 MHz), SOTA activators sending a picture from the summit, ordinary SSTV QSOs. Today a user can only listen and talk.

SSTV is an **active** service (user): a user who holds the station deliberately starts it and stops it. Nothing runs in the background, nothing captures automatically. It reuses the station-side decoder framework built for passive APRS (spec `2026-11-10-passive-services-aprs-design.md` §4, §5, §11).

**Success looks like this:** During an ISS pass a member takes control of a station, clicks "ISS", clicks "Empfang starten", and watches the picture build line by line in the browser. Afterwards the picture is in the station's gallery and can be downloaded. The same member uploads a photo (or generates a test picture), ticks their callsign, picks a mode, previews, presses "Senden", and the station transmits it cleanly from key-up to key-down without the browser having to keep anything alive.

Secondary (user): the repeater list and the scanner panel on the control page take too much room. They become collapsible. The new SSTV section is collapsible too.

## 2. Goals

- SSTV section on the station control page (user), next to repeater list and scanner.
- **RX:** start/stop by the lock holder; decoding **on the station** inside the existing decoder framework (user, after APRS chose the same); live line-by-line build-up in the browser (user); automatic VIS mode detection (call); every received picture saved to the gallery automatically (user), incomplete ones too, marked (call, user agreed).
- **TX:** source is an uploaded photo **or** a generated test picture with callsign, current date and time (user). Two checkboxes: overlay **user callsign** and/or **station callsign**, on test picture and on uploaded photos (user). Preview of exactly what will be sent (call). Station-side TX ownership: the station keys, sends, unkeys on its own (call; closes the gap named in APRS spec §11).
- **Frequency:** RX and TX use the frequency currently set on the module (user). An "ISS" button sets 145.800 MHz (user).
- **Modes (user: "the typical ones"; list is a call):** Martin M1, M2; Scottie S1, S2, DX; Robot 36, 72; PD 90, 120, 180 — for both RX and TX.
- **Gallery** per station: received and sent pictures with station, frequency, mode, time, who started/sent; per-picture PNG download (user).
- Collapsible repeater list, scanner panel and SSTV section (user), collapse state remembered per browser (call).

## 3. Non-goals

- Automatic/background SSTV capture (e.g. scheduled ISS passes). The framework would allow it later; not now.
- Doppler correction, pass prediction, satellite tracking.
- Server-side or browser-side decoding of the Opus stream (rejected, see §5.1).
- Modes beyond the list (Wraase, PD 50/160/240/290, MMSSTV narrow modes, FAX).
- Editing tools for uploaded photos beyond crop-to-fit and the callsign overlay.
- Gallery across all stations (network-wide view). Per station only.

## 4. Anti-goals (would be failure even if it "works")

- A stuck carrier: the station keeps transmitting after the picture ended, after an abort, or after the control link died.
- SSTV running on a station nobody holds, or surviving a lock hand-over.
- ~~Transmitting on the ISS downlink (145.800 MHz).~~ (user, 2026-11-25: removed — a licensed operator decides, and it is allowed.)
- A transmission without any station identification because both checkboxes were off unnoticed.
- Pictures kept forever, or photos stored that were never transmitted.
- Station uplink (LTE with data caps, HAMNET) burdened with audio streams for decoding.

## 5. Architecture

### 5.1 Where things run

| Step | Where | Why |
|---|---|---|
| RX decode (VIS, sync, pixels) | station, sandboxed decoder unit | raw RX audio from PipeWire, no Opus artefacts, no audio upload; same pattern as APRS |
| Live lines → browser | station → control WS → server → browser | `decoder_output reliable:false`, ~3–4 KB/s (APRS spec §11) |
| Final picture storage | server | gallery, permissions, retention |
| TX picture prep (crop/scale to mode, overlays, test picture) | server | Pillow is already a server dependency; preview must match exactly what is sent |
| TX audio synthesis | **server (call)** | keeps the station image free of imaging libs; easy to unit-test; one place for mode timing |
| TX keying + playback | station | station owns PTT for the duration; browser need not keep alive |

TX audio synthesis on the server costs one download per transmission: 8 kHz mono 16-bit is 16 KB/s, so PD180 (~187 s) ≈ 3 MB, Martin M1 (~114 s) ≈ 1.8 MB, Robot 36 ≈ 0.6 MB. Transmissions are rare and deliberate, so this is accepted (call). The builder may compress (e.g. FLAC) if the image already ships a decoder for it.

### 5.2 Radio ownership

Follows APRS spec §4 and the existing lock hooks (`apps/control/lock_hooks.py`):

- Only the **ControlLock holder** with `sstv.use` may start RX or TX.
- Starting SSTV RX stops the repeater scanner. Starting the scanner stops SSTV RX. ("Last one started wins", APRS spec §4.1.)
- Voice PTT by the holder while SSTV RX is running is allowed; the decoder keeps running (it simply hears nothing while the radio transmits) (call).
- SSTV TX pauses SSTV RX if running and resumes it after TX ends (call). During SSTV TX the browser's voice PTT and mic are disabled.
- **Lock released, transferred, preempted, swept, or agent disconnect** → SSTV RX and TX stop immediately. Register via `register_release_hook` / `register_acquire_hook`; the hook stops the `sstv` decoder and any TX session.
- A frequency change by the holder during RX is allowed; the decoder continues on the new frequency (call). During TX, setting changes are refused.
- Passive APRS cannot run while the lock is held, so no extra rule is needed there.
- **Running SSTV RX or TX counts as lock activity** for the idle sweep (`apps/control/lock.py`, like `scan_counts_as_activity`), so watching a 30-min RX without clicking does not lose the lock after 300 s (call; otherwise the ISS scenario breaks).
- **Holder's browser gone** (user): the existing rule stays, the lock is released after `RECONNECT_GRACE_SECONDS` without the holder's browser. The release hook then **ends RX** (partial picture saved as `complete:false`) and **aborts TX immediately** (unkey). SSTV never keeps a station for an absent operator. "TX needs no browser keepalive" means only that no per-second PTT keepalives are needed during a transmission.
- **Agent link drop during RX** (call): the agent stops the SSTV decoder itself on control-WS loss, finalises the partial picture as `sstv_image{complete:false}` into the reliable spool, and delivers it after reconnect. The user restarts RX afterwards. This deliberately differs from APRS ("decoders survive WS outages"), because the server force-frees the lock on agent disconnect and SSTV must never run on a station nobody holds.

### 5.3 RX flow

1. Holder clicks "Empfang starten". Server checks lock + `sstv.use`, then sends `decoder_start{decoder:"sstv", slot, params:{}, max_runtime_s}`. `max_runtime_s` = 30 min (call); the UI shows remaining time and the user can start again.
2. Agent plugin `SstvDecoder` (registered next to APRS in `station_agent/agent.py`) writes the env, starts `oe5xrx-decoder@sstv-slot<N>`, and reads the unit's output over a localhost socket (pattern of `aprs.py`, port scheme chosen by the builder, must not clash with APRS `8100+slot`).
3. Decoder outputs, as `decoder_output`:
   - `kind:"sstv_start"`, `reliable:true` — detected mode, width, height, picture id (agent-generated, unique per station).
   - `kind:"sstv_line"`, `reliable:false` — picture id, row index, RGB row (compact encoding, e.g. base64). Never spooled.
   - `kind:"sstv_image"`, `reliable:true` — picture id, mode, `complete: bool`, the full picture as PNG, heard-at timestamps. This is the **authoritative** picture; live lines are only for display, so a server↔browser drop loses only the live view, and an agent↔server drop ends the RX session but the picture so far still arrives (§5.2).
4. Server: `apps/control/consumers.py` routes `decoder_output` by decoder name (today it always goes to `apps/passive/ingest.py`; this needs a small dispatch by `decoder`). `sstv_start`/`sstv_line` are fanned out to the station's control-page browsers; `sstv_image` is stored as a gallery entry and broadcast so the gallery updates live.
5. Incomplete pictures: when RX is stopped (user, max runtime, lock loss) or sync is lost mid-picture, the decoder emits `sstv_image{complete:false}` with the rows received so far. A picture with fewer than ~10 % of rows is discarded (call).
6. Several pictures in one RX session are each saved separately; after a picture ends the decoder waits for the next VIS.

Size check: the largest RX picture (PD180, 640×496) as PNG must fit the agent spool (~1 MB) and the control-WS frame limit after base64. The builder verifies with a noisy real-world sample; if it does not fit, the final picture is uploaded over HTTPS instead (agent-authenticated, like other agent uploads) and the frame only carries a reference.

### 5.4 TX flow

1. In the SSTV section the holder picks **source** (upload / test picture), **mode**, and the two checkboxes **"Mein Rufzeichen"** (user callsign = `User.username`) and **"Stationsrufzeichen"** (`Station.callsign`; checkbox disabled if blank).
2. Server renders the **preview**: crop-to-fill and scale to the mode's resolution, draw the selected callsigns (legible, high-contrast bar), or render the test picture (callsigns, date, time in UTC and local time, mode name, station name) (call for layout). The preview PNG is exactly what will be sent. Uploads are validated (type, size limit, decoded with Pillow's decompression-bomb guard) and held only for the preview session, never stored (user).
3. **Send guard:**
   - If both checkboxes are off, sending asks for confirmation ("Ohne Rufzeichen senden?") (call; anti-goal "no identification").
   - ~~TX is refused if the current TX frequency is 145.800 MHz ±10 kHz (ISS downlink) (call).~~ (user, 2026-11-25: removed — no ISS-downlink guard; the "ISS" button only tunes the receiver.)
   - TX is refused if the TX frequency is outside the module descriptor's TX `ranges`.
4. Holder presses "Senden". Server synthesises the SSTV audio (VIS + mode, 8 kHz mono s16) and stores it as a short-lived artefact. It then sends a new control frame `sstv_tx_start{slot, tx_id, url, sha256, duration_s}` (frame name for the builder).
5. Agent: downloads the audio (agent-authenticated HTTPS, size and sha256 checked), then takes **station-side TX ownership**: suspends the browser keepalive dead-man for this session, keys PTT via the existing `ptt` capability, plays the audio into the slot's TX node, unkeys. **TX level** (call): the SSTV audio is scaled to the station's calibrated TX ceiling (`station_agent/audio/tx_settings.py`, the RF-safety bound) but does **not** pass the voice band-pass/compressor/limiter (`tx_dsp.py`), which would distort the tones. Because only the agent knows the TX settings, playback runs in the agent (call); the audio is a server-generated, checksum-verified PCM/WAV artefact, so parsing is trivial (no untrusted RF input on the TX side). If the builder prefers a unit, the instance name must match the wrapper regex (e.g. `sstvtx`, not `sstv-tx`) and the gain must be passed in.
6. Agent reports `sstv_tx_state{tx_id, state: downloading|keyed|done|aborted|error, progress}`; the browser shows a progress bar and an abort button (`sstv_tx_stop`).
7. **Hard stop, in the agent, independent of the server:** unkey at `duration_s + 5 s` regardless of playback state; unkey on `sstv_tx_stop`; unkey on control-WS disconnect (call: the operator must always be able to stop a remote transmitter; losing the link means losing that ability, so stop); unkey on unit failure. Every unkey path is tested.
8. On `done`, the server stores the sent picture (the preview PNG) in the gallery, with sender, mode, frequency, time. Aborted transmissions are not stored (call). The audio artefact is deleted.

### 5.5 Station side (linux-image)

- Extend `oe5xrx-decoder-run` with an `sstv)` case (RX) and, if playback runs in the unit, an `sstv-tx)` case.
- RX audio like APRS: `pw-record --raw --target $OE5XRX_RX_NODE --rate <r> --channels 1 --format s16 -` piped into the decoder.
- **Decoder program: builder's choice**, constraints:
  - all modes from §2, VIS auto-detection, slant/sync correction good enough for real ISS pictures;
  - streaming: emits rows while receiving, and a final picture;
  - runs inside the existing sandbox (`MemoryDenyWriteExecute`, `ProtectSystem=strict`, localhost only); `MemoryMax`/`CPUQuota` may be raised per decoder instance if measured necessary (a CM4 has headroom; Opus RX and the agent must not starve);
  - maintained upstream, compatible licence, newest stable version; if nothing fits, a small own decoder (e.g. Python + numpy) is acceptable. GUI programs (QSSTV, slowrx) are only usable if their decode core can run headless.
- Image recipe adds whatever the decoder needs (e.g. `python3-numpy`). The static test (`tests/ota-integration/test_decoder_static.py`) is extended.
- The station-agent pin is bumped in linux-image after the station-manager PR merges (as usual, `pin-station-agent.sh`).

### 5.6 Server side (station-manager)

- New app (e.g. `apps/sstv`) or a module inside `apps/control`; builder's choice. It owns: gallery model, settings, views, the TX preview/synthesis, the lock hooks.
- **Model** (sketch): `SstvImage(station, direction rx|tx, mode, frequency_hz, started_by/sent_by (user, nullable after user deletion), created_at, complete, image)` stored via the default storage (S3 in prod, `upload_to="sstv/"`).
- **Settings:** `SstvSettings.retention_days` default 90, admin-editable, like `AprsSettings` (call: follow the APRS pattern).
- **Retention:** daily removal of pictures older than `retention_days`, including the stored files. Hook into `enforce_retention` (`apps/audit/retention.py` steps) or the APRS-style worker; builder's choice, but it must honour `RETENTION_ENFORCE`.
- **TX synthesis library:** builder's choice; PySSTV is the obvious candidate (check maintenance, licence, newest version, that all §2 modes are supported and timing is correct; it has no Robot 72, which then needs an own encoder). Unit tests decode the synthesised audio again (round-trip with the RX decoder or a reference decoder) for every mode.
- **Collapsible sections:** repeater list (`_repeater_panel.html`), scanner (`_scanner_panel.html`), SSTV section: a header with a toggle, collapsed state in `localStorage` per section, default **collapsed** (call; the complaint was space). Alpine, no inline JS, per the control-page conventions.
- UI work goes through `Skill("frontend-design")`. Strings with English msgids plus German translation in the same PR (i18n spec `2026-11-24-i18n-de-design.md`, Du-Form; German labels quoted in this doc are the intended translations). Number inputs (frequency display) keep forced dot-decimal.

### 5.7 Permissions

New area in `apps/accounts/permissions.py` (call): **`sstv`**: view = Applicant, use = Member, manage = Staff.

- `sstv.view`: see the SSTV section read-only and the gallery, download pictures (user: "schauen darf man").
- `sstv.use` + holding the lock: start/stop RX, upload/preview/send, delete **own** sent pictures (user).
- `sstv.manage`: delete any picture of the station (user: from S up).
- Retention setting: admin (as with APRS settings).
- URL-walk guard and matrix test cover the new views and WS frames.

### 5.8 Privacy

Stored data contains callsigns (personal data) and user photos. Pictures fall under the 90-day retention. The privacy policy (`OE5XRX.github.io/docs/datenschutz.md`) gets a paragraph on SSTV pictures (what is stored, for how long, who sees them), and `PRIVACY_POLICY_VERSION` is bumped so users acknowledge it at next login. The policy text change is a separate PR in OE5XRX.github.io, deployed together with this feature.

## 6. Delivery

- station-manager: one feature branch `feature/sstv`, one PR (spec + plan + code), squash-merged.
- linux-image: one PR (decoder/encoder recipe + wrapper + agent pin bump) after the station-manager PR is merged.
- OE5XRX.github.io: one PR for the policy text.
- Rollout: server deploy first (feature UI hidden for stations whose agent does not advertise an `sstv` decoder; the agent's feature advertisement is extended), then image/OTA.

## 7. Risks & early verification

1. **Decoder quality on real SA818 audio** (8 kHz native, FM, ISS Doppler, noise). Spike on test station .211 early: record a real or replayed SSTV signal, decode it with the candidate decoder, judge by eye against a reference decoder (QSSTV on a PC).
2. **Decoder resources in the sandbox** (CPU/RAM on a CM4 next to the Opus bridge). Measure during the spike.
3. **TX timing accuracy.** SSTV is sensitive to sample-rate errors: 8 kHz synthesis played through PipeWire (48 kHz graph) to the 8 kHz fm_board must not slant. Verify by receiving the station's own transmission with a second receiver and decoding it.
4. **Station-side PTT ownership** is new and safety-relevant. Every unkey path (end, +5 s hard stop, abort, WS drop, unit crash, lock loss, agent restart) has a test; plus one HIL/real check on .211 with a dummy load.
5. **Final-picture size** vs spool and WS frame limits (§5.3).

## 8. Left to the builder

Frame names and shapes; app layout; decoder and encoder libraries (within §5.5/§5.6 constraints); localhost port scheme; artefact storage for TX audio; test-picture layout; whether playback runs in the unit or the agent (not as root on untrusted data); size limits for uploads.

## 9. Testing expectations

- **Agent:** SSTV plugin lifecycle, output parsing (start/line/image), spool behaviour for `sstv_image`, TX session: download + checksum, keying, every unkey path, dead-man suspension and restoration.
- **Server:** decoder-output dispatch by decoder name, lock-hook stop of RX/TX on every lock path, exclusivity with the scanner, permission matrix, send guards (TX range, no-callsign confirmation), preview == sent picture, synthesis round-trip per mode, gallery storage, download, deletion rules, retention.
- **E2E (probe):** simulated agent streams lines and a final picture → browser receives lines, gallery entry appears; TX: browser → server synthesis → fake agent downloads and reports progress → gallery entry on done.
- **linux-image:** static test for the new wrapper case(s) and decoder package.
- **Real/HIL:** .211 spike (§7.1–7.3), then one full RX of an SSTV signal and one TX into a dummy load decoded by a second receiver.
