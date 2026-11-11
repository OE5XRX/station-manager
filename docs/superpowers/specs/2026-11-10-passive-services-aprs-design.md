# Passive Services & APRS RX-iGate — Design

**Date:** 2026-11-10 · **Status:** draft for review · **Repos:** station-manager (server + `station_agent`), linux-image (Direwolf + decoder unit), servers (one new worker service)

Decisions marked **(call)** were made by the designer, not explicitly by the user. Review them first.

---

## 1. Intent

Today a station sits idle when nobody is using it. A user logs in, takes control, operates, and gives control back. Then nothing happens until the next user comes along.

This design uses that idle time. When nobody holds a station for *x* minutes, the server switches it into a **passive service**. The first such service is **APRS receive**: the station listens on 144.800 MHz, decodes the APRS packets it hears, and the server:

- gates them into the **APRS-IS** network as an RX-iGate, and
- shows them in station-manager as a log and a map, per station and network-wide.

Over months this builds up a picture of **where our stations actually hear**, so we can see where coverage is good and where it needs work.

"Passive services" is the umbrella name. APRS is the first service, and more can follow. A related *active* service (SSTV receive, started on purpose by a user) is coming next. It reuses the station-side decoder framework defined here, but it is **not** part of this build (see §11 and project memory `feature/sstv/aprs-alignment`).

**Success looks like this:** A station has been idle for a while and shows "Passive: APRS" on its page. Its APRS tab fills with heard stations on a map. Our stations appear on aprs.fi as individual RX-iGates at their real locations. When a user takes control, APRS stops instantly and the radio switches to a sane hand-over profile, and the user's browser shows the real current settings.

## 2. Goals / Non-goals / Anti-goals

**Goals**
- A generic passive-service framework on the server: global settings, a per-station opt-in, an idle scheduler, and hand-over to and from users.
- APRS RX on the station with Direwolf, decoded **on the station** and sent to the server as packets.
- The server gates fresh packets to APRS-IS, **one iGate login per station**, plus a periodic iGate position beacon.
- UI under "Radio → Passive Dienste → APRS": a network-wide map and log, an admin settings tab, and a per-station APRS tab with a status badge.
- Store-and-forward for packets on unreliable links (HAMNET).
- Coverage data collected from day one as long-lived aggregates.
- An RX health alert: a station in passive APRS that hears nothing for *N* hours raises an alert.

**Non-goals (this iteration)**
- **Transmitting**: no APRS TX, no digipeating, no APRS-IS→RF gating. Later, licensed stations will get TX with a different SSID. The design must not block that, but it is not built now.
- A coverage **heatmap UI** and "where to improve" analysis (phase 2). The data is collected now.
- Weather-station views, heard-list statistics, messaging.
- SSTV (separate project, see §11).
- Several passive services running at the same time on one station.

**Anti-goals (would be a failure even if it "works")**
- Streaming audio from stations to the server. Decoding happens on the station. The user rejected server-side decoding because the station uplinks are fixed lines, LTE with data caps, and HAMNET with heavily fluctuating quality.
- Gating **stale** packets into APRS-IS. Late packets make positions jump backwards on aprs.fi.
- A user taking over a station and getting an ear-splitting open squelch, or a browser showing values the radio is not actually set to.
- Decoder output flooding the station's 64 MB persistent journal, which would push out the boot and OTA logs we rely on for debugging.
- APRS knowledge leaking into the agent. The agent stays generic. It does not know that "APRS needs 2 m".

## 3. Constraints & existing facts (verified against `origin/main` @ 780bb43)

| Fact | Where |
|---|---|
| Server↔agent control channel is a persistent WS. Frames use the envelope `{"v":1,"type":…}`. The agent dispatches in `Broker.handle`. | `station_agent/control_client.py`, `station_agent/broker.py:169-187`, `station_agent/protocol.py` |
| Settings go through `command{slot,module,capability,op,value,request_id}` → `result` + `state`. The server applies a lock check, a capability policy, and a 10 s timeout. | `apps/control/consumers.py` ~949-966, `broker.py:196-282` |
| Control lock: `ControlLock` (station scope) in the DB. Idle 300 s, sweep every 5 s. Acquire, release, transfer, preempt, force_free. | `apps/control/models.py:75-110`, `apps/control/lock.py` |
| **There is no release/acquire hook.** The release sequence (`_stop_scan`, PTT clear, `_bridge_gate`, broadcast) is hand-coded in about 4 places. | `consumers.py` ~1293-1377 |
| The repeater scanner runs in the agent, under the lock. A manual set/PTT holds the scan. | `station_agent/scanner.py`, `broker.py:253-260` |
| Virtual modules plug into the Broker. | `station_agent/router_module.py` |
| RX audio is a PipeWire node per slot. Opus bridge: `pipewiresrc target-object=<node>`, a non-exclusive capture. A second consumer has **not** been tested yet. | `station_agent/audio/opus_bridge.py:51-81`, `router_backend.py:85-101` |
| The agent runs as **root**: `station-agent.service` has no `User=`. | `station_agent/station-agent.service` |
| The agent has **no** offline queue. `_ws_send` drops frames while disconnected. | `control_client.py:180-193` |
| Module discovery per slot (`module list`/`describe`). Descriptors carry frequency `ranges`. Server model `StationModule(station,slot,module_id,descriptor,last_state)`. | `station_agent/slot_discovery.py`, `apps/stations/models` |
| FW: every `setting` supports `module <m> get <cap>`. The SA818 itself is not readable, so FW returns its shadow value. | FW-RemoteStation `include/oe5xrx/module/iface.h:409-422` |
| Station journal is persistent, `SystemMaxUse=64M`. | linux-image `oe5xrx-boot-robustness/files/journald-persistent.conf` |
| Permission registry: area × view/use/manage. UI = reference. Reading is open from Applicant (A) upward. | `apps/accounts/permissions.py`, decision memory `decision/user-roles-visibility` |
| The "Radio" sidebar section exists (Repeaters). The station detail page uses tabs. | `templates/includes/sidebar.html`, `apps/stations/templates/stations/station_detail.html` |

**Prerequisite (separate PR, in flight; this build is sequenced after it merges, the lock-hook refactor of §4 may go first independently) (call):** *state read-back*. Today the agent echoes only the capability that was set. For example, setting `frequency` (which also changes rx/tx in FW) leaves the `rx_frequency`/`tx_frequency` displays stale. The fix: after every settings change, and on (re)connect, the agent reads back **all** `setting` capabilities of the module and sends one full snapshot. The browser already treats `state` as authoritative. Brief: project memory `feature/state-readback/brief`, branch `fix/agent-state-readback`. This design **relies** on it. Hand-over and passive-profile changes must show up correctly in the browser.

## 4. Who owns the radio (general rule)

This is a station-wide rule. It is not APRS-specific, and SSTV and future services use it too.

1. The **ControlLock holder** owns the radio. Their activities are voice/PTT, the repeater scanner, and later SSTV. These activities exclude each other: the last one started wins and stops the previous one. This extends today's "manual set/PTT holds the scan".
2. The **passive scheduler** may use the radio **only** while the lock has been free for at least `idle_minutes`.
3. **Lock acquire** of any kind (acquire, transfer, preempt/override, request→grant) **stops passive services first**, then applies the **hand-over profile** (§6.3).
4. **Lock release**, sweep expiry, or agent disconnect/force_free starts the idle timer. When it runs out, the scheduler starts the passive service again. "APRS pauses and resumes by itself."
5. A listener **without** the lock (control.view, Opus RX) does **not** block passive services. While a passive service runs (squelch 0 = constant noise), the control page's **browser-side RX mixer starts muted** and shows a notice ("Passiv: APRS läuft, Audio stumm"); the listener can unmute to listen in (user decision). Purely client-side: no server-side audio gating, the Opus stream itself is unchanged.
6. Agent offline means nothing runs. When the agent reconnects, the scheduler re-evaluates.

**Required refactor (small, part of this build):** fold the hand-coded release and acquire sequences in `apps/control/consumers.py` into **one** `on_lock_released(station, reason)` / `on_lock_acquired(station, holder, reason)` pair. Every existing path calls it. Passive services, and later SSTV, hook in there. The existing behaviour (stop scan, clear PTT, bridge gate, broadcast, audit) stays identical. This needs a regression test per path.

## 5. Station side

### 5.1 Decoder framework in the agent

- The agent gets a small **decoder host**. Each decoder (APRS now, SSTV later) is a plugin with `start(slot, params)`, `stop()`, `status()`, and an output emitter. An optional `tx()` is reserved for later and not built.
- The agent knows decoders by name. It knows **nothing** about bands or frequencies. The server tunes the radio through the normal `command` path before it starts a decoder.
- New control-WS frames (exact shape is the builder's choice, intent below):
  - ↓ `decoder_start{decoder, slot, params, request_id, max_runtime_s?}`
  - ↓ `decoder_stop{decoder, slot, request_id}`
  - ↑ `decoder_state{decoder, slot, state: starting|running|stopped|error, detail}`, sent on every transition and in the (re)connect snapshot.
  - ↑ `decoder_output{decoder, slot, seq, kind, reliable, data}` (see §5.4)
  - Start and stop are acked with a timeout, like `command`. The agent enforces `max_runtime_s` (needed by SSTV; for APRS it is unset).
- On agent start no decoder runs. Only the server starts decoders.

### 5.2 Process management: systemd template unit

The agent runs as root, and decoders parse **untrusted RF input** (anyone can transmit packets). So decoders do **not** run as agent child processes. Instead:

- linux-image ships **one** template unit, `oe5xrx-decoder@.service`. The instance name is `<decoder>-slot<N>`, e.g. `aprs-slot1`. Instances are created on demand by `systemctl start`. There is nothing fixed per slot.
- The unit runs as a dedicated unprivileged user with PipeWire group access. It uses sandboxing (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, …), resource caps (`MemoryMax`, `CPUQuota`) so a runaway decoder cannot starve audio or the agent, and `Restart=on-failure`.
- **Logging:** `LogLevelMax=notice` plus `LogRateLimitIntervalSec`/`Burst`. Direwolf runs quiet: packets go only over KISS, never to stdout. Only start, stop and errors reach the journal.
- Parameters come from an env file the agent writes to `/run/oe5xrx/decoder/<instance>.env` (tmpfs, gone after reboot). A wrapper script picks the PipeWire RX node of slot N and runs the decoder.
- The agent starts and stops instances with `systemctl` and reports unit failures as `decoder_state=error`. If a module is unplugged, the agent stops that slot's instances.

### 5.3 APRS decoder

- **Direwolf** (user decision, over writing our own: its decode rate is the product). It is built from a Yocto recipe. Check whether an existing layer has one, otherwise write a small recipe (CMake). It is GPLv2, a separate program.
- Audio: `pw-record` from the slot's RX node, piped into Direwolf on stdin, at the PipeWire graph rate with no Opus in the path. It must coexist with the Opus RX bridge on the same node. **Verify early on the test station .211** (§10).
- Direwolf exposes KISS on localhost. The agent's APRS plugin reads KISS frames, timestamps each at reception (`heard_at`), and emits them as `decoder_output{kind:"ax25", reliable:true}`.
- Direwolf is configured RX-only: no PTT and no beacons are configured.

### 5.4 Store-and-forward

- `reliable:true` outputs go into a **bounded in-RAM spool** in the agent, e.g. a few thousand packets or about 1 MB **(call)**. When full, the oldest are dropped and a counter is kept.
- The server acks by `seq`, and the agent drops acked entries. After a reconnect the agent resends the unacked ones. The server de-duplicates by `(station, decoder, seq-epoch, seq)`.
- Nothing is spooled to flash. A reboot loses the spool, which is acceptable **(call)**.
- `reliable:false` (SSTV live lines later) is never spooled.

## 6. Server side

### 6.1 Passive-service framework

- **Global settings per service** (admin only), stored in the DB and editable in the UI:
  - enabled (global switch)
  - `idle_minutes`
  - **service radio profile**: the required **band**, plus capability values to apply. For APRS the defaults are 144.800 MHz, squelch 0, no CTCSS/tones, wide bandwidth. Empty fields keep the module's value.
  - service-specific settings (APRS: §6.4)
- **Global hand-over profile** (admin only): the capability values applied when a user takes over a station on which a passive service was running. Defaults: **145.500 MHz, squelch 1, no tones**. Empty fields are left unchanged. This was the user's choice over "restore persisted values" (explicit and predictable; note that `volume` *is* persisted since `capability_policy.py:39-40`, the choice stands for predictability).
- **Per station**: an opt-in checkbox "takes part in passive services". It is off by default. A station without it behaves exactly as today. **For APRS, enabling it requires the admin to set an iGate callsign and SSID on the same form** (user decision; the free-text `Station.callsign` is not used for APRS-IS, because several stations may share the club call). Validation: valid amateur callsign format, SSID 0–15, `callsign-SSID` unique across all stations. The form pre-fills SSID 10 for RX-only as a hint only.
- **Slot selection:** the scheduler matches the service's required band against the frequency `ranges` in the module descriptors (`StationModule`) and picks the **first free matching slot** **(call)**. It does not hard-code "fm" or "slot 1". If no module matches, the service does not run, and the UI says why.
- **Scheduler:** a server-side loop. The existing control sweep (5 s, `consumers.py` ~342-357) or a dedicated task are both fine (builder's choice). For each opted-in, online station whose lock has been free ≥ `idle_minutes` and with no passive service running, it applies the service radio profile via the normal command path, waits for the results, and then sends `decoder_start`. On failure it backs off and retries later, and the error shows in the station status.
- **State:** a per-station `PassiveServiceRun`-like record (service, slot, state, started_at, last_error) drives the badge and the UI.
- **System-issued commands (call):** passive and hand-over profile commands are issued by the server as a system actor (audit actor `system:passive_services`). They skip the user lock/verb checks (the lock is free by definition for passive; for hand-over the new holder has just been granted). Profiles may contain tuning caps (frequencies, tones, squelch, bandwidth, power) **and `volume`** (user decision: the RX level must be tuned for Direwolf, so the service profile sets it and the read-back shows it). Scheduler/system writes are **never persisted**: they do not touch the station's persisted calibration. On hand-over the station's **persisted** `volume` is re-applied (unless the hand-over profile sets one explicitly), so listeners get the station's calibrated level back. Filter caps (`filter_*`) stay out of profiles.
- **Not during drain/OTA (call):** a station is not eligible while module-firmware drain is active (`lock.is_draining`) or while it is `UPDATING` (OTA). Entering either state stops a running passive service the same way as a lock acquire, but without applying the hand-over profile.
- On `on_lock_acquired`: `decoder_stop` → apply the hand-over profile → close the run record. The lock grant must **not** wait for the decoder stop to complete: preemption stays immediate, and the stop is best effort with a timeout.

### 6.2 Packet ingest & storage

- On `decoder_output{kind:"ax25"}` the server parses the AX.25/APRS frame, stores it (raw plus parsed fields: source, destination, path, type, position if any, comment), acks the `seq`, and broadcasts it to the live UI groups.
- **Retention:** raw packets are deleted after a configurable period, default **30 days** (user: "don't keep everything forever"). A daily cleanup task does this.
- **Duplicates across stations:** the same packet heard by several of our stations is stored **per station**, which the coverage data needs. The network-wide log **groups** it ("heard by -211, -205").

### 6.3 Coverage aggregates (collected now, visualised in phase 2)

- On ingest, packets with a position that were **heard directly** update a long-lived aggregate. "Directly" means no digipeater has marked the path as used, i.e. no `*` on a path element. Digipeated packets say nothing about our station's own reach and **must be excluded**, otherwise coverage looks better than it is.
- The aggregate is a grid cell (about 2 km; H3, Maidenhead subsquare, or a simple lat/lon grid are the builder's choice) × iGate station, with count, first_heard, last_heard and max distance. It contains no callsigns.
- Aggregates are **not** subject to raw-packet retention. They are tiny and kept indefinitely.

### 6.4 APRS-IS gateway (server only; stations never talk to APRS-IS)

- A **long-running worker** process (a new service in the deployment, e.g. a `manage.py` command container in `servers`). It cannot live in request handling.
- **One APRS-IS login per station** (user decision), as the station's configured **iGate callsign-SSID** (per station, §6.1) with a passcode computed from the callsign. A different SSID will be used for future RX+TX stations (set by the admin then). The worker keeps one TCP connection per station with an active passive APRS run, opening and closing it as runs start and end. Server, port and filter are global settings (rotate.aprs2.net:14580 as default **(call)**).
- **Freshness gate:** a packet is gated only if `now - heard_at ≤ max_gate_age`, default 30 s and configurable. Older packets (e.g. delivered after a HAMNET outage) are stored and shown but **never gated**.
- **Gating rules:** standard RX-iGate rules. Don't gate packets with `TCPIP`, `TCPXX`, `NOGATE` or `RFONLY` in the path, don't gate third-party packets without unwrapping per the APRS-IS spec, and don't gate our own iGate callsigns.
- **iGate beacon:** every 30 min **(call)**, an APRS-IS-only position packet per active iGate, using the station's coordinates, symbol "R&" (RX-only iGate), and a comment like "OE5XRX Remote Station RX-iGate". This needs no RF transmission and no TX licence.
- A station without a valid callsign or coordinates: packets are logged locally, nothing is gated or beaconed, and the settings UI shows a warning.

### 6.5 RX health alert

- A station in a passive APRS run that has received **zero** packets for `no_rx_alert_hours` (global, default 6 h **(call)**) raises an alert through the existing monitoring/alert routing ("possible antenna/RX problem"). The alert resolves on the next packet. Runs shorter than the threshold never alert.

### 6.6 Live frequency/state in the browser

- Whatever changes the radio (passive profile, hand-over profile, scanner, other users) must reach every open control page as a `state` snapshot. The prerequisite PR covers the agent side. This build must make sure the passive and hand-over paths go through the normal command path, so they get the read-back for free, and that the control page shows a passive-service badge ("Passive: APRS on slot N").

## 7. UI

Layout **variant C** (user decision):

- **Sidebar** "Radio → Passive Dienste → APRS". "Passive Dienste" is a section, so later services slot in beside APRS.
- **APRS page** (central):
  - Tab **Karte & Log**: a map of heard stations across all iGates (default last 24 h), and a live log, newest first, with a station filter and grouped duplicates.
  - Tab **iGates**: our stations with their state (running/idle/in use/error/not opted-in), last packet, and packets per 24 h.
  - Tab **Einstellungen** (admin only): global switch, `idle_minutes`, service radio profile, hand-over profile, APRS-IS server/filter, `max_gate_age`, retention, `no_rx_alert_hours`.
- **Station detail**: a new tab **APRS** for opted-in stations. It is the same map and log, pre-filtered to this station. The station's opt-in checkbox plus iGate callsign and SSID live in the station edit form (admin).
- **Status badge** (station detail and control page): `Passiv: APRS aktiv` / `in Benutzung` / `aus` / `Fehler: …`.
- Map library: whatever the project already uses or a light one, e.g. Leaflet with OSM tiles. Builder's choice, follow existing static-asset/CSP conventions.
- UI work follows the project rules: `Skill("frontend-design")`, `{% comment %}` instead of multi-line `{# #}`, `lang="en"` on numeric inputs, and existing tokens/styles.

## 8. Permissions

A new area **`passive_services`** in `apps/accounts/permissions.py`:

- **view = A (Applicant)**: map, logs, iGate status, badges. APRS data is public anyway. This follows "areas visible to all, actions gated".
- **manage = Admin**: settings, hand-over profile, per-station opt-in.
- Lock and override rules for the radio are unchanged (`control.*`).

## 9. Protocol & compatibility

- New frames are additive. Old servers ignore unknown agent frames, and old agents ignore unknown server frames (the existing forward-compat behaviour). The server must handle an agent that does not support decoders: it gets no `decoder_state`, or a start times out → mark it "agent too old", don't retry hot. Advertise support in the inventory/hello (`capabilities: ["decoders"]` or similar, builder's choice).
- Deploy order: server first (feature dormant), then the image with Direwolf, the decoder unit and the new agent (SRCREV bump via `scripts/pin-station-agent.sh`), then per-station opt-in.

## 10. Risks & early verification

1. **Second PipeWire consumer on the RX node** next to the Opus bridge. Spike on .211 at the start: run Opus RX and `pw-record` → Direwolf in parallel and check that neither drops out and that Direwolf decodes real 144.800 traffic.
2. **Direwolf decode rate with SA818 audio** (8 kHz native, upsampled in the graph; squelch 0). Measure packets per hour on .211 against a reference (aprs.fi for the area).
3. **Lock-hook refactor regressions.** This is covered by per-path tests before any passive logic hooks in.
4. **APRS-IS etiquette.** The freshness gate, path rules and one login per station must be right before the first live login. Test against an APRS-IS test port or a filter-only connection first.

## 11. Alignment with SSTV (next project, not built here)

From `feature/sstv/aprs-alignment`. This design deliberately provides:

- the generic decoder host, the start/stop/state/output frames and `max_runtime_s`;
- `reliable:false` outputs for the live line stream (about 3–4 KB/s fits the control WS);
- the radio-ownership rule (§4), where SSTV is a lock-holder activity exclusive with scanner and voice;
- the single lock hook pair.

SSTV still needs on its own: TX (station-side PTT ownership, since today's PTT is browser-keepalive driven), image storage and gallery, and its decoder program and recipe.

## 12. Open for the builder

- Exact frame shapes and names, model/table design, the grid scheme, and the map library.
- Scheduler placement (control sweep vs. a dedicated task) and its back-off policy.
- APRS parsing library (e.g. `aprslib`; check maintenance and licence and pick the newest stable version) versus a small own parser for the fields we need.
- Direwolf recipe source (existing layer vs. own).
- Spool size limits and the iGate beacon interval within the stated intent.

## 13. Testing expectations

- **Agent:** decoder host lifecycle (start/stop/error/max_runtime), the systemd invocation (mocked), KISS frame → `decoder_output`, spool bounds/ack/resend/de-dup, and no decoder started on boot.
- **Server:** each lock path calls the hook (regression first), scheduler eligibility (opt-in, idle, online, band match, lock free), the hand-over sequence, ingest, retention cleanup, direct-vs-digipeated classification, coverage aggregation, the freshness gate, gating path rules, passcode, beacon content, the alert raise/resolve, and the permission matrix (URL-walk guard covers new views).
- **E2E (probe, as usual):** a simulated agent sends KISS-derived frames → server stores, aggregates and broadcasts → a fake APRS-IS socket receives exactly the fresh, gateable packets in the right format.
- **HIL/real:** the .211 spike in §10, then a 24 h soak on .211 with the user's opt-in.
- **linux-image:** static test that the unit file has the sandboxing/log-limit directives, that Direwolf is in the image, and that the decoder user exists.
