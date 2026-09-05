# Server Audio Relay — Component Design (Session C)

**Status:** Draft for review
**Date:** 2026-09-05
**Parent:** `docs/superpowers/specs/2026-09-03-audio-subsystem-design.md` (Spec 0 — NORMATIVE wire contract)
**Repo:** `station-manager` (server / Django Channels), Django 6, Python 3.14, asyncio
**Scope:** Session C only — the server relay. No agent (Session B, merged #121), no web/mixer (Session D).

This design conforms to Spec 0 §5 byte-for-byte and validates against the shared §5.7
fixtures (`tests/fixtures/audio/*`), so it interoperates with the Session-B agent without
having been built together.

---

## 1. Guiding principle: the server is a dumb relay

Per Spec 0 §3/§5: the server **authenticates both ends, relays + fans out opaque Opus
frames, and enforces lock/PTT gating on the uplink**. It performs **no decode, no mix, no
encode**. Binary media frames are forwarded **byte-identically** (the original `bytes` are
re-emitted; the §5.3 header is parsed only to read `stream_ref` for routing, never rewritten).

## 2. New Django app: `apps/audio`

A cohesive new app (mirrors `apps/control`, `apps/tunnel`) holding the two consumers, WS
routing, the per-station gating/subscription state (pure sync ops + models), and constants.

| File | Purpose |
|---|---|
| `apps/audio/apps.py` | `AudioConfig` (added to `INSTALLED_APPS`) |
| `apps/audio/models.py` | `AudioGate` (per-station gate) + `AudioSubscription` (per-connection demand row) |
| `apps/audio/gate.py` | Pure, synchronous gate ops (analog `apps/control/lock.py`) |
| `apps/audio/subscriptions.py` | Pure, synchronous demand-counting ops |
| `apps/audio/consumers.py` | `AgentAudioConsumer` + `AudioConsumer` |
| `apps/audio/routing.py` | `websocket_urlpatterns` (browser) + `agent_websocket_urlpatterns` (agent) |
| `apps/audio/constants.py` | version, group-name helpers, PTT TTL |
| `apps/audio/migrations/0001_initial.py` | AudioGate + AudioSubscription |

Wiring: `config/asgi.py` (add audio routes to the browser + agent URLRouters),
`config/settings/base.py` (`INSTALLED_APPS += apps.audio`).

## 3. Endpoints & auth (Spec 0 §5.1)

- **Agent:** `ws/agent/audio/<station_id>/` → `AgentAudioConsumer`.
  Ed25519 query-param auth, **identical** to `AgentControlConsumer._verify_agent`
  (signed string `"{timestamp}:{sha256('')}"`, 60 s skew, current+next key, station bound to
  the DeviceKey — NOT the client-supplied id). Added to the `/ws/agent/` URLRouter, so it
  skips `AllowedHostsOriginValidator` (matches control/terminal).
- **Browser:** `ws/audio/<station_id>/` → `AudioConsumer`.
  Django session/OIDC; `can_use_station(station)` required to connect (listen). Uplink (mic)
  additionally requires holding the `ControlLock` **and** PTT active (§5.5, enforced server-side).

Reject shape mirrors the existing consumers: agent → pre-accept `close(code)`; browser →
accept-then-`{type:"error"}`-then-close so the client sees a reason.

## 4. Groups & fan-out (Spec 0 §5.5)

Channel-layer groups per station (works across workers via the prod Redis layer; the
InMemory layer covers tests):

- `audio_<station>_agent` — the agent connection(s). Server→agent JSON
  (`source_subscribe`/`source_unsubscribe`/`mic_state`) and uplink media frames are
  group-sent here.
- `audio_<station>` — all browser connections; used for gate/stream-state broadcasts.
- `audio_<station>_src_<stream_ref>` — per-source fan-out. A browser joins on `subscribe`
  and leaves on `unsubscribe`/disconnect. The agent's RX media frame for `stream_ref` is
  fanned out to this group → **one agent encode shared to N browsers** (§5.5 downlink).

Binary passthrough over the channel layer: `{"type":"audio.media","data":<bytes>}`; the
handler re-emits `self.send(bytes_data=event["data"])` — byte-identical.

### op.mic producer semantics (§5.2)

`op.mic` media is produced by the operator's browser (uplink). On an authorized mic frame
the server (a) forwards it to `audio_<station>_agent` (TX injection) **and** (b) fans it out
to `audio_<station>_src_<op.mic ref>` subscribers — so a listener who subscribed to `op.mic`
hears the operator. Same fan-out mechanism as a station-produced source.

## 5. stream_ref mapping (§5.2/§5.3)

The server reads the `stream_ref ↔ stream_id` mapping from the agent's `advertise`
(each entry carries an explicit `stream_ref`; the server MUST NOT infer it from array index —
Session-B amendment). The map is cached on the `AgentAudioConsumer` and rebuilt on every
`advertise`. `subscribe`/`unsubscribe` from browsers reference `stream_id`s; the server
resolves them to `stream_ref` via the map and (a) joins/leaves the `_src_<ref>` group and
(b) drives demand gating. A browser that subscribes before the first `advertise` (empty map)
is parked and reconciled when `advertise` arrives.

## 6. Demand gating (§5.5)

`AudioSubscription(station, stream_id, channel_name)` — one row per (browser connection,
stream). `subscriptions.subscribe(...)` inserts and returns `first=True` iff the count for
that `(station, stream_id)` went 0→1; `unsubscribe(...)`/`drop_channel(...)` delete and return
`last=True` iff it went 1→0. On `first` the server sends `source_subscribe{stream_id}` to the
agent group; on `last` it sends `source_unsubscribe{stream_id}`. `op.mic` is **not**
demand-subscribed at the agent (the agent does not produce it) — only station-produced
sources are. Disconnect deletes all rows for the channel and emits `source_unsubscribe` for
each source that hit zero. DB-row counting (not in-memory) is worker-safe and mirrors the
`TerminalSession` precedent.

## 7. Uplink gating & the cross-plane bridge (§5.5)

The uplink gate is **`(sender holds ControlLock) AND (PTT active) AND (tx_route set)`** —
all server-authoritative. Lock + PTT + tx_route live on **different WS planes** (lock/PTT/
tx_route on `/ws/control`; mic media on `/ws/audio`), so the server needs shared state:

**`AudioGate(station OneToOne)`** — `ptt_active`, `ptt_slot`, `ptt_module`, `tx_slot`,
`tx_module`, `ptt_expires_at`. Pure ops in `gate.py`: `set_ptt`, `refresh_ptt`, `clear_ptt`,
`set_tx_route`, `clear_tx_route`, `get(station)`, `mic_allowed(station, user)` (holder==user
AND ptt_active AND now<expires). Dead-man: `ptt_expires_at = now + AUDIO_PTT_TTL` (default
3 s > the 1 s control keepalive); mic_allowed treats an expired gate as PTT-off.

**Control-plane glue (minimal, additive, lazy-imported to avoid an app cycle) in
`apps/control/consumers.py::ControlConsumer`:**

- `_handle_command` with `capability=="ptt"`: `value` truthy → `gate.set_ptt(slot,module)`;
  falsy → `gate.clear_ptt()`. Then broadcast `audio.gate` to `audio_<station>` + `_agent`.
- `receive` `ptt_keepalive` (holder): `gate.refresh_ptt()` + broadcast (keeps the audio
  dead-man alive at the 1 s cadence).
- `_handle_command` with `capability=="tx_route"` (after the existing lock check + relay):
  `gate.set_tx_route(value)` / `clear_tx_route` on null; broadcast.
- Any lock-clearing / holder-changing path (release, transfer, preempt, sweep, force_free on
  agent disconnect, grace expiry): `gate.clear_ptt()` + broadcast (the old holder can no
  longer keep TX keyed). Reuses the existing `_broadcast_lock` call sites.

The existing generic command path **already** lock-gates + relays `tx_route` to the agent and
**already** upserts/serves the `audio-router` virtual-module descriptor via
`registry.apply_inventory`/`snapshot` (synthetic slot 1000). So §5.6 needs **only** the
tx_route snoop above — no new relay/descriptor code. `tx_route` stays on `/ws/control`.

**Audio-plane consumption of the gate:** both consumers cache the gate in memory, seeded by
one DB read at connect, refreshed by `audio.gate` broadcasts (no per-frame DB hit for the
~50 fps mic stream). On a gate change the `AgentAudioConsumer` re-sends `mic_state`
(`{active, tx_slot, tx_module}`) to the agent so it knows to expect/inject (or stop) mic
media — `active = ptt_active AND tx_route set AND a holder exists`.

Uplink frame handling in `AudioConsumer.receive(bytes_data=...)`: if `mic_allowed` → forward
to agent group + fan out to `op.mic` subscribers; else **drop the frame + send
`{type:"error","code":"not_locked",...}`** (throttled to avoid spamming one error per dropped
20 ms frame — one error per closed→open transition).

## 8. Message handling summary

**Agent → server:** `advertise` (rebuild ref map; relay `streams` to `audio_<station>` +
send current `source_subscribe` for any already-demanded sources), `stream_state` (relay to
browsers). Binary media frame → parse header for `stream_ref` → fan out to
`_src_<ref>` byte-identically.

**Server → agent:** `source_subscribe`/`source_unsubscribe` (demand), `mic_state` (gate),
uplink `op.mic` media frames (byte-identical).

**Browser → server:** `hello` (capabilities, ack/no-op), `subscribe`/`unsubscribe`
(join/leave `_src_<ref>` + demand), `mic_open`/`mic_close` (declare/close uplink format;
server validates it can be relayed — lock+PTT — else `error`). Binary frame → uplink gate.

**Server → browser:** `streams` (from advertise), `stream_state` (relayed), `error`
(`not_locked|not_authorized|unknown_stream|format_unsupported`). Downlink media frames.

## 9. §5.4 contract reconciliation (this PR)

Spec 0 §5.4 said "Receivers MUST use FEC data when `flags` bit0 is set", contradicting the
§5.3 amendment (bit0 advisory; MVP leaves it 0; in-band FEC lives inside the Opus packet).
Reconciled: bit0 is an **optional hint** (may be 0 even when FEC is present); loss detection
is via `seq`; the receiver uses Opus in-band FEC / PLC at decode time **independent of the
bit**. The server never inspects `flags` (opaque relay), so this is receiver (Session D)
guidance only. One doc edit in the same PR.

## 10. Test strategy (TDD, Spec 0 §5.7 / §7 Session-C row)

Channels consumer tests via `WebsocketCommunicator` + `asyncio.run` inside
`@pytest.mark.django_db(transaction=True)` (mirrors `tests/test_control_consumer_relay.py`).
An `audio_agent_auth` conftest fixture monkeypatches `AgentAudioConsumer._verify_agent`
(as `control_agent_auth` does). Opaque Opus frames come from `media_frame_slot0rx.bin`;
**no real audio.**

- **Auth:** agent Ed25519 accept + reject (bad/expired/missing sig); browser session +
  `can_use_station` accept + anon/forbidden/unknown-station reject. (Ed25519 path unit-tested
  directly against a real `DeviceKey` for one accept + one reject, since the fixture bypasses it.)
- **advertise → streams** relay with correct explicit `stream_ref` (from `advertise.json`).
- **subscribe → source_subscribe** to agent on first subscriber (demand) + **fan-out to TWO
  browsers** of the same agent frame; **unsubscribe/disconnect → source_unsubscribe** at zero.
- **Uplink gating:** mic frame with no lock / lock-but-no-PTT → **dropped + `error`**, nothing
  reaches the agent; with lock+PTT(+tx_route) → **relayed to agent + fanned to op.mic
  subscribers**; `mic_state` to the agent reflects lock+PTT+tx_route transitions.
- **Byte-identical passthrough:** parse the relayed/fanned frame with `station_agent.audio.frame`
  and assert the emitted bytes equal the source bytes exactly.
- **Reconnect/backoff:** agent disconnect → gate PTT cleared + subscriptions reaped; browser
  reconnect re-subscribes cleanly (no leaked demand).
- **Control-plane glue:** a `tx_route` command on `/ws/control` (lock-held) updates the gate +
  drives `mic_state.tx_slot`; PTT on/off + keepalive drive gate state; lock loss clears PTT.

## 11. Out of scope

Browser JS/mixer/WebCodecs (Session D), agent (Session B), station-local idle services /
occupancy / cross-band links (Spec 0 §11 — seams honored, not built), QUIC leg (Phase 2 / F).
