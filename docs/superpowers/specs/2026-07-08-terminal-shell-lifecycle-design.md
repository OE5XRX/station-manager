# Terminal Shell Lifecycle & Robustness — Design

**Date:** 2026-07-08
**Status:** Approved (brainstorming complete, pending implementation plan)
**Repos touched:** `station-manager` (server + frontend + agent code), `linux-image` (SRCREV bump → OTA)

## Problem

The web terminal (`/ws/terminal/<station_id>/`) has three overlapping robustness bugs, surfaced while debugging a persistent `[ disconnected: 1006 ]`:

1. **Transient browser disconnect kills the shell permanently.** `TerminalConsumer.disconnect()` sends `terminal_close` to the agent on *every* browser WS close (tab switch, iOS backgrounding, network blip). The agent's `_handle_message("close")` calls `_stop_shell()`. The agent only spawns a shell **once**, in `_connect_and_serve()` (on agent-WS connect), so it is **never respawned**. After a transient disconnect + reconnect the browser shows `[ connected ]` but there is no live shell → no prompt, keystrokes go nowhere.

2. **Zombie `TerminalSession` rows lock the station out.** `connect()` creates a `TerminalSession` (status `connecting`) *before* `group_add`/`accept`. If the connection aborts before `disconnect()` runs cleanly (server kill, the pre-fix Redis timeout, unclean close), the row is never closed. `_count_active_sessions()` counts status in `(connecting, active)`; once it reaches `MAX_SESSIONS_PER_STATION` (2) every new browser connect is rejected with `4429` — a permanent lockout. Observed live: 2 `active` rows from April 2026 blocking station 2.

3. **All pre-accept rejections look identical (`1006`) to the browser.** Every reject in `connect()` (`4401/4403/4404/4409/4429`) happens *before* `accept()`, so Daphne closes the handshake with no WS close frame → the browser's `onclose` always sees `1006`. The real reason (offline / not authorized / too many sessions) is invisible, which made diagnosis very slow.

Additionally the browser terminal has **no auto-reconnect** (`app.js` just prints `[disconnected]`), and terminal access is gated on `is_internal` (staff + admin) despite being effectively a **root shell** on the station.

## Goals

- A transient browser disconnect must **not** destroy the shell; the browser **reattaches** on reconnect.
- If the shell is dead when a browser (re)connects, the agent **auto-respawns** it (never show a dead terminal).
- A **manual "Restart shell"** control lets the operator recover a *hung-but-alive* shell.
- Rejections surface a **human-readable reason** in the terminal, not a bare `1006`.
- The browser terminal **auto-reconnects** with backoff.
- Terminal access is restricted to **admins**.
- The zombie-session lockout can no longer happen.

## Non-Goals (documented follow-ups)

- Topology-based terminal access (see Access Control §; end-state is admin **and** topology, we ship admin-only now).
- Scrollback replay on reattach (blank screen until next output is acceptable for now).
- The infra/observability follow-ups from the 1006 incident (runner `Restart=`, OOM root-cause, runner-offline alert, channel-layer health check, explicit `protocol=2` on the channel layer). Tracked as separate issues.

## Behavior Model (chosen: "C" — persist + auto-recover + manual restart)

| Event | Behavior |
|---|---|
| Transient browser disconnect | Shell **persists**. No `terminal_close` sent to agent. |
| Browser (re)connect | Server sends `terminal_ensure`; agent spawns a shell **only if none is alive** (reattach otherwise). |
| Shell dead at connect (crashed / `exit` / agent-WS was fresh) | Auto-respawned via `terminal_ensure` → fresh prompt, no user action. |
| Shell hung (alive but unresponsive) | Operator clicks **Restart** → `terminal_restart` → agent kills + respawns. |
| Shell dies while browser attached (`exit`, crash) | Browser shows `[ closed: <reason> ]`. **No** auto-respawn-in-place (avoids crash loops); recovery happens on next connect or via Restart. |

## Message Protocol (in-band on the existing terminal WS)

Transport decision: **in-band on the terminal channel** (not the D3 control-WS). The shell is a terminal concern; the terminal WS already carries `input`/`resize`/`close`.

New messages:

| Direction | Message | Trigger | Effect |
|---|---|---|---|
| Browser → `TerminalConsumer` | `{type:"restart"}` | Restart button | `group_send(terminal_<id>_agent, {type:"terminal_restart"})` |
| `TerminalConsumer` → agent group | `{type:"terminal_ensure"}` | Browser connect (after `accept`) | Agent ensures a shell is alive |
| `AgentTerminalConsumer` handler | `terminal_ensure` / `terminal_restart` | from group | forward to agent WS as `{type:"ensure"}` / `{type:"restart"}` |
| Server → browser | `{type:"error", reason, code}` | operational reject | browser shows reason, then WS closes |

Existing messages (`input`, `resize`, `close`, `output`, `closed`) are unchanged. `close` (explicit user end-session) still kills the shell; it is **no longer sent on transient disconnect**.

## Component Changes

### Agent — `station_agent/terminal.py`

Decouple shell lifecycle from the WS connection:

- `_ensure_shell()` — start a shell + reader task **only if** `self._process is None` or the process has exited; otherwise no-op. Idempotent.
- `_restart_shell()` — `_stop_shell()` (if alive) then start fresh + new reader task.
- `_connect_and_serve()` — calls `_ensure_shell()` on connect (instead of unconditionally spawning once). The `async for message` loop dispatches the new message types.
- `_handle_message()` — add `"ensure"` → `_ensure_shell()`, `"restart"` → `_restart_shell()`. Keep `"close"` → `_stop_shell()`.
- Reader-task ownership moves to the ensure/restart helpers so each shell instance has exactly one reader. A shell that exits on its own is **not** reaped by the reader; `_ensure_shell()` detects the dead process via `poll()` and runs `_stop_shell()` to clean up before respawning. A reader cancelled intentionally (restart / ensure reaping a dead shell) stays silent — it emits no `{type:"closed"}` frame.

### Server — `apps/tunnel/consumers.py`

`TerminalConsumer.connect()`:
- **`accept()` first**, then run ALL checks (auth + operational). Every reject uses accept-then-error `send({type:"error", reason, code})` then `close(code)` so the browser shows the reason — including the anonymous/unauthenticated case (`4401`), so no reject is an opaque `1006` and the client stops reconnecting. Checks: authenticated (else 4401), **admin** gate (else 4403), station found (4404), station online (4409), session limit (4429).
- **Admin gate:** replace `user.is_internal` with `user.is_admin` (see Access Control). Non-admin authenticated user → `{type:"error", reason:"Terminal access is restricted to admins"}` + close.
- **Create the `TerminalSession` only after `accept()` and after checks pass** — a pre-accept/auth failure leaves no zombie row.
- After success: `group_add`, then `group_send(terminal_<id>_agent, {type:"terminal_ensure"})` so the agent guarantees a live shell.

`TerminalConsumer.disconnect()`:
- **Remove** the `group_send(terminal_close)` to the agent. Close only the DB session + audit + `group_discard`. The shell survives.

`TerminalConsumer.receive()`:
- add `restart` → `group_send(terminal_<id>_agent, {type:"terminal_restart"})`.
- `close` (explicit) still forwards `terminal_close`.

`AgentTerminalConsumer`:
- add handlers `terminal_ensure` and `terminal_restart` forwarding `{type:"ensure"}` / `{type:"restart"}` to the agent WS.

### Session staleness — `apps/tunnel` (fix #2 / bug 2)

- **Order fix:** session row created only post-`accept` (above) → no `connecting` zombies from failed handshakes.
- **Staleness guard:** add a `last_seen` timestamp to `TerminalSession`, refreshed by a **periodic keepalive task in `TerminalConsumer`** that runs while the WS is open (e.g. every 60s), **not** by user I/O — so an open-but-idle terminal (user not typing) stays fresh and is never wrongly reaped. `_count_active_sessions()` counts only rows with `last_seen` within `TERMINAL_SESSION_STALE_TTL` (must be > keepalive interval; default ~3 min, exact value in the plan). Rows older than the TTL are (a) excluded from the count and (b) marked `closed` in a connect-time reconciliation sweep for that station. When the WS dies uncleanly the keepalive stops → the row goes stale after the TTL and frees the slot.
- Net effect: a browser that dies uncleanly frees its slot after the TTL; the April-zombie permanent lockout is impossible; a legitimately idle open session is never closed.

### Frontend — `static/js/app.js` + station detail template

Frontend work follows the `frontend-design` skill.

- **Restart button** in the terminal panel → `ws.send({type:"restart"})`; write a `\x1b[33m[ restarting shell… ]\x1b[0m` marker; optionally `term.reset()`.
- **Reject-reason display:** on `{type:"error"}` message, render the human reason (e.g. "Station offline", "Too many sessions", "Terminal access is restricted to admins") instead of relying on the close code.
- **Auto-reconnect:** on WS `close` that is not an explicit user close, reconnect with capped exponential backoff (e.g. 1s→…→15s), showing `[ reconnecting… ]`. On reopen the server's `terminal_ensure` guarantees a live shell → seamless recovery from tab-switch/network blips. Stop after N failed attempts with a clear message + manual "reconnect" affordance.

## Access Control

- **Now:** admin-only (`user.is_admin`). Root shell → highest privilege.
- **End-state (documented, not this PR):** admin **and** topology — a user may open a terminal only on stations within a region/station they are assigned to (via the `stations` topology model). Ships as a follow-up when topology responsibilities are built out.
- Audit logging on session open/close is retained.

## Error Handling

- Agent: `_ensure_shell`/`_restart_shell` guard against double-spawn and against races between reader-task cancellation and respawn. A shell that fails to spawn sends `{type:"closed", reason:"shell failed to start"}` to the browser.
- Server: ALL rejects (including anonymous/unauthenticated, 4401) `accept()`-then-`error`-then-`close` so the browser gets a reason and never an opaque 1006.
- Frontend: bounded reconnect attempts; explicit terminal states (connecting / connected / reconnecting / closed / error).

## Deployment & OTA Sequencing

Two-part rollout (the server half already fixes the reported iOS case with the *current* agent):

1. **Server + frontend** (`station-manager` image → GHCR → prod redeploy via `servers` `main.yml`). Removing the `terminal_close`-on-disconnect alone makes the shell survive transient disconnects with the existing agent. Reject-reason, auto-reconnect, admin gate, session-staleness all land here.
2. **Agent** (`station_agent/terminal.py`) → bump SRCREV in `linux-image` (`scripts/pin-station-agent.sh`) → Yocto image rebuild → OTA to the station. Enables `ensure` auto-respawn + the `restart` button end-to-end.

The implementation plan sequences these so item 1 ships first for quick relief.

## Testing

- **Server (Django Channels, in-memory layer):** connect creates no zombie on pre-accept/auth failure; disconnect does **not** send `terminal_close`; `restart`/`ensure` group-sends fire; operational rejects send `{type:"error"}` then close; admin gate; `_count_active_sessions` ignores stale rows + reconciliation closes them.
- **Agent (pytest):** `_ensure_shell` spawns only when dead; `_restart_shell` kills + respawns; shell persists across `input`/`resize`; reader task is singular per shell; a cancelled reader emits no `closed` frame.
- **Integration (`probe`, full E2E):** browser input → server → agent → PTY → output; restart flow; transient-disconnect → reattach; dead-shell → auto-respawn on connect.
- **Frontend:** restart button, reconnect backoff, reject-reason rendering.

## Follow-ups (tracked separately, from the 1006 incident)

1. Explicit `protocol=2` (RESP2) in `CHANNEL_LAYERS` as belt-and-suspenders beyond the `redis<8.0` pin.
2. Channel-layer ping in the web health check (web was "healthy" during a total WS outage).
3. Runner `Restart=on-failure` in `servers` cloud-init (OOM-killed 2026-07-04, never auto-restarted → 4 days of silent no-deploy).
4. OOM root-cause: image provisioning (libguestfs/TCG) on the prod CX23 + 86% disk — move heavy provisioning off the prod VM / add swap / cgroup the runner.
5. Alert on "self-hosted runner offline > N min" (reuse existing email/telegram alerting).
6. Scrollback replay buffer on reattach.
