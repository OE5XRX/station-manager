# Session C Handback — Server Audio Relay (Django Channels)

**Date:** 2026-09-05
**Branch:** `feat/server-audio-relay` (off `origin/main` @ 5c9cc74, which contains Session B #121)
**PR:** #<PR> (see below) — **NOT merged. Marked for review.**
**Scope:** server relay only (Session C). No agent (B, merged), no web/mixer (D).
**Status:** Full suite green (1363 passed, 1 skipped), ruff clean, migrations clean, two-stage review (atlas spec → audit quality) passed. Copilot-loop pending on the open PR.

---

## 1. What was built

New Django app **`apps/audio`** — the dumb Opus relay + fan-out per Spec 0 §3/§5. The
server authenticates both ends, relays opaque media frames byte-identically, and enforces
lock+PTT gating on the uplink. No decode/encode/mix.

| File | Role |
|---|---|
| `apps/audio/consumers.py` | `AgentAudioConsumer` (`/ws/agent/audio/<id>/`, Ed25519) + `AudioConsumer` (`/ws/audio/<id>/`, session + `can_use_station`) |
| `apps/audio/gate.py` | Per-station gate ops (PTT/tx_route/dead-man) + `get_wire_state` (msgpack-safe broadcast) + `mic_allowed` |
| `apps/audio/subscriptions.py` | DB-backed demand counting (first/last, row-locked per station) |
| `apps/audio/models.py` | `AudioGate` (OneToOne station), `AudioSubscription` (per-connection demand row) |
| `apps/audio/constants.py` | protocol version, group-name helpers, PTT TTL, `OP_MIC_STREAM_ID` |
| `apps/audio/routing.py` | browser + agent WS urlpatterns |
| `apps/audio/migrations/0001_initial.py` | AudioGate + AudioSubscription |

Wiring: `config/asgi.py` (audio routes into browser + agent URLRouters), `config/settings/base.py`
(`apps.audio` in INSTALLED_APPS). Control-plane glue in `apps/control/consumers.py` (additive,
lazy-imported): PTT/tx_route snoop + lock-loss → `AudioGate` bridge broadcasts.

## 2. How the contract maps (Spec 0 §5)

- **§5.1 auth:** agent Ed25519 verify is a verbatim copy of `AgentControlConsumer` (60 s skew,
  current+next key, station bound to the URL station's active DeviceKey); browser session +
  `can_use_station`; uplink additionally requires the `ControlLock`. Agent route skips
  `AllowedHostsOriginValidator`; browser route through `AuthMiddlewareStack`.
- **§5.2/§5.3:** JSON signaling relayed with exact envelopes; `stream_ref` read from the
  explicit `advertise` field (never array index). Binary frames relayed **byte-identical**
  (header parsed only to route by `stream_ref`; never rewritten).
- **§5.5 gating:** downlink to any `can_use_station` conn via per-source groups
  (`audio_<st>_src_<stream_id>`) — one agent encode fanned to N browsers; uplink relayed to the
  agent **and** fanned to `op.mic` subscribers only while holder+PTT (dead-man TTL); demand
  gating sends `source_subscribe` at first browser subscriber, `source_unsubscribe` at zero.
  `op.mic` is browser-produced → never demand-subscribed at the agent (§5.2).
- **§5.6 control-plane:** `tx_route` stays on `/ws/control`, lock-gated by the **existing**
  generic command path; the `audio-router` descriptor (synthetic slot 1000) flows through the
  **existing** registry — no new relay/descriptor code. The only glue added is a tx_route
  **snoop** (so the server can build `mic_state.tx_slot`, with a PTT-module fallback) + PTT
  bridging + clear-PTT on every lock-loss path.

## 3. Cross-plane design (the non-obvious part)

Lock/PTT/tx_route live on `/ws/control`; mic media on `/ws/audio`. Shared authoritative state
= **`AudioGate`** (per-station). The control consumer writes it (PTT on/off, keepalive refresh,
tx_route, clear-on-lock-loss) and broadcasts a **msgpack-safe `audio.gate`** wire state to the
audio groups. Browser consumers cache it in memory (seeded once at connect, refreshed by
broadcasts) so the uplink decision is a **pure in-memory check** — no per-frame DB at ~50 fps.
The agent consumer turns each gate change into a `mic_state`.

**Late-join fix:** the agent advertises once (connect/hotplug); a browser connecting afterwards
would never learn the streams. On connect the browser sends `audio.request_advertise` (with its
own `reply_channel`) to the agent group; the agent consumer replays its cached `_last_streams`
to just that browser (mirrors `ControlConsumer`'s connect-time inventory snapshot). Cross-worker
safe.

## 4. Contract fix taken (Spec 0 §5.4)

Reconciled the §5.4 "Receivers MUST use FEC when bit0 set" wording with the §5.3 amendment:
bit0 is an **optional hint** (MVP leaves it 0; in-band FEC lives inside the Opus packet), loss
is detected via `seq`, and receivers use Opus in-band-FEC/PLC at decode time **independent of
the bit**. The server never inspects `flags` (opaque relay); this is receiver (Session D)
guidance. One doc edit, same PR.

## 5. Okay-gate evidence (real test output)

```
# full audio + E2E + control-regression (5× flakiness sweep — deterministic)
pytest tests/test_audio_consumer.py tests/test_audio_server_relay_e2e.py \
       tests/test_control_consumer_relay.py tests/test_control_consumer_lock.py
  → 47 passed  (×5)

# whole repository suite (no collateral breakage)
pytest -q  → 1363 passed, 1 skipped in 132.71s

ruff check apps/audio/ apps/control/consumers.py tests/  → All checks passed!
manage.py makemigrations --check --dry-run              → No changes detected
```

Test coverage (`tests/test_audio_consumer.py` 30+ + `tests/test_audio_server_relay_e2e.py` 6):
agent Ed25519 accept + reject (incl. real-keypair sign/verify + auth-reject teardown), browser
session/`can_use_station` accept + anon/forbidden/unknown rejects, advertise→streams with
explicit `stream_ref`, demand `source_subscribe` once on first + fan-out to **two** browsers
byte-identical (parsed via `station_agent.audio.frame`), unsubscribe/disconnect →
`source_unsubscribe` at zero, uplink drop+`error` without lock/PTT vs relay+op.mic fan-out with
lock+PTT, `mic_state` mirrors lock+PTT+tx_route (with PTT-module fallback), **msgpack-safety of
the gate broadcast** (prod `channels_redis` regression), subscribe-before-advertise reconcile,
stream_id validation, disconnect demand reaping.

## 6. Review trail (agent-team flow, Major Feature)

- **Round 1 (gateway, TDD):** built the app + tests.
- **Independent controller verification** caught a **prod-breaking msgpack-datetime** bug in the
  gate broadcast (InMemory test layer masked it) + per-frame-DB uplink + a mic_state tx-target
  gap + a group-add-before-auth smell → fixed (gate cache/wire-state, cache-based uplink,
  tx fallback, join-after-auth).
- **Round 1.5 code-simplifier:** 2 surgical dedups.
- **Round 2 watchers — guard (security) + audit (quality):** 1 BLOCKER
  (`AgentAudioConsumer.disconnect` AttributeError on auth-reject) + MAJORs (unvalidated
  `stream_id` → channel-group crash; subscribe first/last race under READ COMMITTED; missing
  gate-broadcast on agent drop) + MINORs → all fixed (`select_for_update` per-station anchor,
  `stream_id` regex + list cap, call-time TTL, hardened test helpers, added tests).
- **Round 2.5 probe:** 6-test E2E (`tests/test_audio_server_relay_e2e.py`) — no product bugs.
  Fixing a resulting flaky test surfaced the real late-join gap (fixed via re-advertise).
- **Round 3 two-stage review — atlas (spec §5) → audit (quality):** atlas PASS-WITH-CONDITIONS
  → fixed the 2 conditions (op.mic demand exemption drift vs Session B; §5.2 error-enum for
  connect rejects). Final audit: no BLOCKER/MAJOR; 2 benign MINORs left (see §7).

## 7. Deviations / open points

- **Station-not-found reject** uses the in-enum `not_authorized` error code (numeric 4404 stays
  in the WS close frame) — chosen to stay within the §5.2 enum and not leak station existence.
- **Dual-agent reconnect race** could send a browser two identical `streams` replies (idempotent;
  matches the existing control-consumer pattern). Not guarded.
- **`format_unsupported`** error code is defined but not emitted (the MVP relay treats media as
  opaque and does not validate `mic_open` format — that lands with Session D's WebCodecs).
- **`AUDIO_PTT_TTL_SECONDS`** default 3.0 s (> the 1 s control keepalive). Configurable via
  settings (resolved at call time).
- No real audio/HW here (that's Session E E2E/HIL); everything is validated against the §5.7
  fixtures with opaque Opus frames, exactly as the contract intends for B↔C interop.

## 8. Not done (out of scope by design)

Browser JS / mixer / WebCodecs (Session D), station-local idle services + occupancy + cross-band
(Spec 0 §11 — seams honored, not built), QUIC datagram leg (Phase 2 / Session F).
