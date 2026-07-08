# Terminal Shell Lifecycle & Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the web terminal survive transient browser disconnects (persist + reattach), auto-recover / manually restart a dead-or-hung shell, show human-readable reject reasons instead of a bare `1006`, auto-reconnect the browser, gate access to admins, and end the zombie-`TerminalSession` lockout.

**Architecture:** In-band control on the existing terminal WebSocket. The agent's shell lifecycle is decoupled from the WS connection (`_ensure_shell`/`_restart_shell`). The server stops killing the shell on transient disconnect, sends `terminal_ensure` on browser connect, forwards a `restart` control message, accepts-then-errors on operational rejects, and reaps stale sessions via a per-connection keepalive + TTL.

**Tech Stack:** Django 6.0 + Django Channels (Redis channel layer), Python 3.14, `websockets` (agent), xterm.js + vanilla JS (frontend), pytest / pytest-django (server), pytest (agent).

## Global Constraints

- Terminal access is **admin-only** (`user.is_admin`), replacing the previous `user.is_internal` gate. (Topology-based access is a documented future step, NOT this plan.)
- Control transport is **in-band on the terminal WS** — do NOT use the D3 `/ws/agent/control/` channel.
- ALL rejects (including anonymous/unauthenticated `4401`, plus offline / session-limit / not-admin) must `accept()` then send `{type:"error", reason, code}` then `close(code)` — so no reject reaches the browser as an opaque `1006` and the client stops reconnecting.
- A transient browser disconnect must NOT send `terminal_close` to the agent.
- Session staleness uses a **periodic keepalive while the WS is open** (NOT user I/O) so an idle-but-open terminal is never reaped. TTL must be greater than the keepalive interval.
- Django template comments: only `{% comment %}`, never multi-line `{# … #}`.
- **Tests live FLAT in `tests/` (e.g. `tests/test_terminal_lifecycle.py`) — NOT in `tests/tunnel/` or `tests/agent/`.**
- **This repo has NO `pytest-asyncio`.** Async code is tested with plain sync test functions that call `asyncio.run(coro())` — follow the established pattern in `tests/test_terminal_agent.py` and `tests/test_control_client.py`. Do NOT use `@pytest.mark.asyncio` or a module-level `pytestmark = pytest.mark.asyncio`. For Channels consumer tests, create the DB objects with normal ORM in the sync test body (under `@pytest.mark.django_db(transaction=True)`), then drive the `WebsocketCommunicator` inside a nested `async def scenario(): ...` executed via `asyncio.run(scenario())`. The test-code blocks below that use `@pytest.mark.asyncio` are ILLUSTRATIVE of intent only — port them to the `asyncio.run` pattern.
- DE-locale number inputs are irrelevant here; no numeric form fields added.
- Agent (`station_agent/terminal.py`) ships to stations via `linux-image` SRCREV bump + OTA — it is NOT deployed by the station-manager image. Server + frontend deploy via the station-manager image.

---

## File Structure

- `station_agent/terminal.py` — **modify**: decouple shell lifecycle; add `_ensure_shell`, `_restart_shell`; handle `ensure`/`restart` messages.
- `tests/test_terminal_lifecycle.py` — **create**: agent shell-lifecycle unit tests.
- `apps/tunnel/models.py` — **modify**: add `last_seen` to `TerminalSession`.
- `apps/tunnel/migrations/000X_terminalsession_last_seen.py` — **create**: migration.
- `apps/tunnel/consumers.py` — **modify**: `TerminalConsumer.connect/disconnect/receive` + keepalive + staleness helpers; `AgentTerminalConsumer` new handlers.
- `tests/test_terminal_consumer.py` — **create/extend**: Channels tests.
- `static/js/app.js` — **modify**: restart button wiring, reject-reason rendering, auto-reconnect.
- `apps/stations/templates/stations/station_detail.html` — **modify**: add Restart button to the terminal panel.

Constants live at the top of `apps/tunnel/consumers.py`:
```python
MAX_SESSIONS_PER_STATION = 2          # existing
TERMINAL_SESSION_KEEPALIVE_SECONDS = 60
TERMINAL_SESSION_STALE_TTL_SECONDS = 180   # must be > keepalive
```

---

## Task 1: Agent shell lifecycle — ensure / restart (`station_agent/terminal.py`)

**Files:**
- Modify: `station_agent/terminal.py`
- Test: `tests/test_terminal_lifecycle.py` (create)

**Interfaces:**
- Produces (agent-internal): `async _ensure_shell()` (spawn only if no live shell), `async _restart_shell()` (stop + fresh spawn), `_shell_alive() -> bool`. Reader-task ownership held on `self._reader_task`.
- Consumes: existing `_start_shell()`, `_stop_shell()`, `_read_shell_output()`.
- Wire protocol (from server): incoming WS messages `{type:"ensure"}` and `{type:"restart"}` in addition to existing `input`/`resize`/`close`.

- [ ] **Step 1: Write failing tests for shell-alive detection and ensure/restart**

Create `tests/test_terminal_lifecycle.py`:
```python
import asyncio
import pytest
from unittest.mock import MagicMock, patch
from station_agent.terminal import TerminalClient
from station_agent.config import AgentConfig


def _client():
    cfg = MagicMock(spec=AgentConfig)
    cfg.ed25519_key_path = "/nonexistent"
    cfg.terminal_shell = "/bin/sh"
    # Bypass key loading in __init__
    with patch("station_agent.terminal.load_private_key", return_value=MagicMock()):
        return TerminalClient(cfg)


def test_shell_alive_false_when_no_process():
    c = _client()
    assert c._shell_alive() is False


def test_shell_alive_true_when_process_running():
    c = _client()
    proc = MagicMock()
    proc.poll.return_value = None  # still running
    c._process = proc
    c._master_fd = 5
    assert c._shell_alive() is True


def test_shell_alive_false_when_process_exited():
    c = _client()
    proc = MagicMock()
    proc.poll.return_value = 0  # exited
    c._process = proc
    c._master_fd = 5
    assert c._shell_alive() is False


@pytest.mark.asyncio
async def test_ensure_shell_spawns_when_dead():
    c = _client()
    c._ws = MagicMock()
    started = {"n": 0}

    def fake_start():
        started["n"] += 1
        proc = MagicMock(); proc.poll.return_value = None
        return 7, proc

    with patch.object(c, "_start_shell", side_effect=fake_start), \
         patch.object(c, "_read_shell_output", return_value=asyncio.sleep(0)):
        await c._ensure_shell()
        await c._ensure_shell()  # second call: shell alive -> no-op
    assert started["n"] == 1


@pytest.mark.asyncio
async def test_restart_shell_stops_then_starts():
    c = _client()
    c._ws = MagicMock()
    calls = []
    proc = MagicMock(); proc.poll.return_value = None
    c._process = proc; c._master_fd = 7

    async def fake_stop():
        calls.append("stop"); c._process = None; c._master_fd = None

    def fake_start():
        calls.append("start")
        p = MagicMock(); p.poll.return_value = None
        return 8, p

    with patch.object(c, "_stop_shell", side_effect=fake_stop), \
         patch.object(c, "_start_shell", side_effect=fake_start), \
         patch.object(c, "_read_shell_output", return_value=asyncio.sleep(0)):
        await c._restart_shell()
    assert calls == ["stop", "start"]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_terminal_lifecycle.py -v`
Expected: FAIL — `_shell_alive` / `_ensure_shell` / `_restart_shell` do not exist.

- [ ] **Step 3: Add lifecycle helpers and `_reader_task` bookkeeping**

In `station_agent/terminal.py`, add `self._reader_task = None` in `__init__` (after `self._master_fd`). Then add these methods (place near `_stop_shell`):
```python
    def _shell_alive(self) -> bool:
        """True iff a shell process exists and has not exited."""
        return self._process is not None and self._process.poll() is None

    async def _ensure_shell(self):
        """Start a shell + reader task only if none is currently alive.

        Idempotent: a no-op when a shell is already running, so a browser
        reattach (server sends ``ensure`` on every connect) does not spawn
        duplicates.
        """
        if self._shell_alive():
            return
        # Clean up a dead-but-not-reaped process/fd before respawning.
        if self._process is not None or self._master_fd is not None:
            await self._stop_shell()
        self._master_fd, self._process = self._start_shell()
        self._reader_task = asyncio.create_task(self._read_shell_output(self._master_fd))

    async def _restart_shell(self):
        """Kill the current shell (if any) and start a fresh one."""
        await self._cancel_reader()
        await self._stop_shell()
        self._master_fd, self._process = self._start_shell()
        self._reader_task = asyncio.create_task(self._read_shell_output(self._master_fd))

    async def _cancel_reader(self):
        """Cancel and await the current reader task, if any."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_terminal_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 5: Wire `ensure`/`restart` into `_handle_message` and use `_ensure_shell` in `_connect_and_serve`**

In `_handle_message`, add branches before the final `else` (after the `close` branch, lines ~253-255):
```python
        elif msg_type == "ensure":
            await self._ensure_shell()

        elif msg_type == "restart":
            logger.info("Terminal: restart requested")
            await self._restart_shell()
```

Replace the shell-start block in `_connect_and_serve` (current lines ~298-302):
```python
            # Start the shell
            self._master_fd, self._process = self._start_shell()

            # Start the output reader as a background task
            reader_task = asyncio.create_task(self._read_shell_output(self._master_fd))
```
with:
```python
            # Ensure a shell is running (idempotent). The reader task is
            # owned by _ensure_shell via self._reader_task.
            await self._ensure_shell()
```
And update the `finally` block of `_connect_and_serve` (current lines ~311-318): replace the `reader_task.cancel()/await` dance with `await self._cancel_reader()`:
```python
            finally:
                await self._cancel_reader()
                await self._stop_shell()
                self._ws = None
```

- [ ] **Step 6: Add a message-dispatch test for ensure/restart/input**

Append to `tests/test_terminal_lifecycle.py`:
```python
@pytest.mark.asyncio
async def test_handle_message_dispatches_ensure_and_restart():
    c = _client()
    ensure_called = restart_called = 0

    async def fake_ensure():
        nonlocal ensure_called; ensure_called += 1

    async def fake_restart():
        nonlocal restart_called; restart_called += 1

    with patch.object(c, "_ensure_shell", side_effect=fake_ensure), \
         patch.object(c, "_restart_shell", side_effect=fake_restart):
        await c._handle_message('{"type":"ensure"}')
        await c._handle_message('{"type":"restart"}')
    assert ensure_called == 1 and restart_called == 1
```

- [ ] **Step 7: Run full agent terminal test module**

Run: `pytest tests/test_terminal_lifecycle.py -v`
Expected: PASS (all).

- [ ] **Step 8: Commit**

```bash
git add station_agent/terminal.py tests/test_terminal_lifecycle.py
git commit -m "feat(agent): decouple terminal shell lifecycle (ensure/restart)"
```

---

## Task 2: `TerminalSession.last_seen` field + migration

**Files:**
- Modify: `apps/tunnel/models.py`
- Create: `apps/tunnel/migrations/000X_terminalsession_last_seen.py` (generated)

**Interfaces:**
- Produces: `TerminalSession.last_seen: DateTimeField(null=True, db_index=True)` — refreshed by the consumer keepalive; used by `_count_active_sessions` staleness filter.

- [ ] **Step 1: Add the field**

In `apps/tunnel/models.py`, after `started_at` (line 34) add:
```python
    last_seen = models.DateTimeField(_("last seen"), null=True, blank=True, db_index=True)
```

- [ ] **Step 2: Generate the migration**

Run: `python manage.py makemigrations tunnel`
Expected: creates `apps/tunnel/migrations/000X_terminalsession_last_seen.py` adding `last_seen`.

- [ ] **Step 3: Verify migration applies**

Run: `python manage.py migrate tunnel --plan` then `python manage.py migrate tunnel`
Expected: applies cleanly, no errors.

- [ ] **Step 4: Commit**

```bash
git add apps/tunnel/models.py apps/tunnel/migrations/
git commit -m "feat(tunnel): add TerminalSession.last_seen for staleness reaping"
```

---

## Task 3: `TerminalConsumer.connect()` rewrite — accept-then-error, admin gate, create-after-accept, ensure-on-connect, staleness

**Files:**
- Modify: `apps/tunnel/consumers.py` (`TerminalConsumer.connect`, `_count_active_sessions`, add helpers, constants)
- Test: `tests/test_terminal_consumer.py` (create/extend)

**Interfaces:**
- Consumes: `TerminalSession.last_seen` (Task 2); agent handler `terminal_ensure` (Task 4) — the connect sends `{"type":"terminal_ensure"}` to `f"{group}_agent"`.
- Produces: reject protocol — on operational failure the browser receives `{"type":"error","reason":<str>,"code":<int>}` immediately followed by WS close. Helpers `_reject(code, reason)`, `_count_active_sessions()` (stale-aware), `_reap_stale_sessions()`, `_touch_session()`, keepalive task on `self._keepalive_task`.

- [ ] **Step 1: Write failing Channels tests**

Create/extend `tests/test_terminal_consumer.py`:
```python
import pytest
from channels.testing import WebsocketCommunicator
from channels.db import database_sync_to_async
from django.utils import timezone
from datetime import timedelta
from config.asgi import application
from apps.tunnel.models import TerminalSession

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


async def _connect(user, station_id=1):
    comm = WebsocketCommunicator(application, f"/ws/terminal/{station_id}/")
    comm.scope["user"] = user
    return comm


@database_sync_to_async
def _make_station(status="online"):
    from apps.stations.models import Station
    return Station.objects.create(name="s", status=status)


@database_sync_to_async
def _make_user(level):
    from apps.accounts.models import User
    return User.objects.create(username=f"u_{level}", membership_level=level)


async def test_non_admin_gets_error_message_then_close(settings):
    st = await _make_station()
    user = await _make_user("staff")  # internal but NOT admin
    comm = await _connect(user, st.id)
    connected, _ = await comm.connect()
    # accept happened, then an error message arrives, then close
    msg = await comm.receive_json_from()
    assert msg["type"] == "error"
    assert msg["code"] == 4403
    await comm.disconnect()


async def test_offline_station_gets_error(settings):
    st = await _make_station(status="offline")
    user = await _make_user("admin")
    comm = await _connect(user, st.id)
    await comm.connect()
    msg = await comm.receive_json_from()
    assert msg["type"] == "error" and msg["code"] == 4409
    await comm.disconnect()


async def test_stale_sessions_do_not_block(settings):
    st = await _make_station()
    user = await _make_user("admin")
    # Two ancient "active" rows with old last_seen -> must be reaped, not counted
    await database_sync_to_async(TerminalSession.objects.create)(
        station=st, user=user, status="active",
        last_seen=timezone.now() - timedelta(hours=5))
    await database_sync_to_async(TerminalSession.objects.create)(
        station=st, user=user, status="active",
        last_seen=timezone.now() - timedelta(hours=5))
    comm = await _connect(user, st.id)
    connected, _ = await comm.connect()
    assert connected is True  # not rejected with 4429
    await comm.disconnect()
    reaped = await database_sync_to_async(
        lambda: TerminalSession.objects.filter(station=st, status="closed").count())()
    assert reaped >= 2
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_terminal_consumer.py -v`
Expected: FAIL — current code rejects pre-accept (no `error` message), uses `is_internal`, counts stale rows.

- [ ] **Step 3: Add constants + staleness helpers**

At the top of `apps/tunnel/consumers.py` (after `MAX_SESSIONS_PER_STATION = 2`):
```python
TERMINAL_SESSION_KEEPALIVE_SECONDS = 60
TERMINAL_SESSION_STALE_TTL_SECONDS = 180  # must exceed the keepalive interval
```
Add imports at top: `import asyncio` and `from datetime import timedelta`.

Add these helpers to `TerminalConsumer` (near the DB helpers):
```python
    @database_sync_to_async
    def _reap_stale_sessions(self):
        """Close sessions whose keepalive has lapsed (dead WS never cleaned up)."""
        from apps.tunnel.models import TerminalSession

        cutoff = timezone.now() - timedelta(seconds=TERMINAL_SESSION_STALE_TTL_SECONDS)
        TerminalSession.objects.filter(
            station_id=self.station_id,
            status__in=("connecting", "active"),
        ).filter(models.Q(last_seen__lt=cutoff) | models.Q(last_seen__isnull=True)).update(
            status="closed",
            ended_at=timezone.now(),
            close_reason="stale (keepalive lapsed)",
        )

    @database_sync_to_async
    def _touch_session(self):
        if self.session:
            self.session.last_seen = timezone.now()
            self.session.save(update_fields=["last_seen"])

    async def _keepalive_loop(self):
        try:
            while True:
                await asyncio.sleep(TERMINAL_SESSION_KEEPALIVE_SECONDS)
                await self._touch_session()
        except asyncio.CancelledError:
            raise
```
Add `import` for `models` if not present: `from django.db import models`.

- [ ] **Step 4: Rewrite `connect()` (accept-then-error, admin gate, reap, create-after-accept, ensure, keepalive)**

Replace the body of `TerminalConsumer.connect()` with:
```python
    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"terminal_{self.station_id}"
        self.session = None
        self.keepalive_task = None

        user = self.scope.get("user")
        # Unauthenticated: reject pre-accept (no friendly message needed).
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return

        # Accept first so operational rejects can send a readable reason
        # (a pre-accept close reaches the browser only as an opaque 1006).
        await self.accept()

        # Root shell -> admin only.
        if not user.is_admin:
            await self._reject(4403, "Terminal access is restricted to admins")
            return

        station = await self._get_station()
        if station is None:
            await self._reject(4404, "Station not found")
            return
        if station.status != "online":
            await self._reject(4409, "Station is offline")
            return

        await self._reap_stale_sessions()
        if await self._count_active_sessions() >= MAX_SESSIONS_PER_STATION:
            await self._reject(4429, "Too many active terminal sessions for this station")
            return

        # Only now create the tracking row — a failed/rejected handshake
        # never leaves a zombie "connecting" session behind.
        self.session = await self._create_session(user)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self._update_session_status("active")
        await self._touch_session()
        self.keepalive_task = asyncio.create_task(self._keepalive_loop())

        # Ask the agent to guarantee a live shell (spawns if dead, reattach otherwise).
        await self.channel_layer.group_send(
            f"{self.group_name}_agent", {"type": "terminal_ensure"}
        )

        await self._audit_log(
            station, "updated", f"Terminal session opened by {user.username}", user
        )

    async def _reject(self, code, reason):
        """Accept-then-error reject so the browser shows a real reason."""
        try:
            await self.send(text_data=json.dumps({"type": "error", "reason": reason, "code": code}))
        finally:
            await self.close(code=code)
```

- [ ] **Step 5: Make `_count_active_sessions` stale-aware**

Replace `_count_active_sessions` body:
```python
    @database_sync_to_async
    def _count_active_sessions(self):
        from apps.tunnel.models import TerminalSession

        cutoff = timezone.now() - timedelta(seconds=TERMINAL_SESSION_STALE_TTL_SECONDS)
        return TerminalSession.objects.filter(
            station_id=self.station_id,
            status__in=("connecting", "active"),
            last_seen__gte=cutoff,
        ).count()
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `pytest tests/test_terminal_consumer.py -v`
Expected: PASS (non-admin error, offline error, stale-do-not-block).

- [ ] **Step 7: Commit**

```bash
git add apps/tunnel/consumers.py tests/test_terminal_consumer.py
git commit -m "feat(tunnel): admin-gated terminal, accept-then-error rejects, stale-session reaping"
```

---

## Task 4: `disconnect()` / `receive()` + `AgentTerminalConsumer` handlers

**Files:**
- Modify: `apps/tunnel/consumers.py`
- Test: `tests/test_terminal_consumer.py` (extend)

**Interfaces:**
- Consumes: keepalive task `self.keepalive_task` (Task 3).
- Produces: browser `{type:"restart"}` → group_send `{"type":"terminal_restart"}` to agent group; `AgentTerminalConsumer.terminal_ensure`/`terminal_restart` forward `{"type":"ensure"}`/`{"type":"restart"}` to the agent WS. `disconnect()` no longer sends `terminal_close`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_terminal_consumer.py`:
```python
async def test_disconnect_does_not_close_shell(settings):
    """Browser disconnect must NOT tell the agent to close the shell."""
    st = await _make_station()
    user = await _make_user("admin")
    # Spy on the agent group
    from channels.layers import get_channel_layer
    layer = get_channel_layer()
    await layer.group_add(f"terminal_{st.id}_agent", "agent-spy")
    comm = await _connect(user, st.id)
    await comm.connect()
    # drain the terminal_ensure sent on connect
    await layer.receive("agent-spy")
    await comm.disconnect()
    # No terminal_close should arrive within a short window
    import asyncio as _a
    with pytest.raises(_a.TimeoutError):
        await _a.wait_for(layer.receive("agent-spy"), timeout=0.3)


async def test_browser_restart_forwards_terminal_restart(settings):
    st = await _make_station()
    user = await _make_user("admin")
    from channels.layers import get_channel_layer
    layer = get_channel_layer()
    await layer.group_add(f"terminal_{st.id}_agent", "agent-spy2")
    comm = await _connect(user, st.id)
    await comm.connect()
    await layer.receive("agent-spy2")  # drain ensure
    await comm.send_json_to({"type": "restart"})
    msg = await layer.receive("agent-spy2")
    assert msg["type"] == "terminal_restart"
    await comm.disconnect()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_terminal_consumer.py -k "restart or does_not_close" -v`
Expected: FAIL — disconnect still sends `terminal_close`; no `restart` handling.

- [ ] **Step 3: Rewrite `disconnect()` (no shell kill; cancel keepalive)**

Replace `TerminalConsumer.disconnect()`:
```python
    async def disconnect(self, close_code):
        if getattr(self, "keepalive_task", None):
            self.keepalive_task.cancel()
            try:
                await self.keepalive_task
            except asyncio.CancelledError:
                pass
            self.keepalive_task = None

        if self.session:
            await self._close_session(close_reason=f"disconnect (code={close_code})")
            station = await self._get_station()
            if station:
                user = self.scope.get("user")
                await self._audit_log(
                    station, "updated",
                    f"Terminal session closed by "
                    f"{user.username if user and not user.is_anonymous else 'unknown'}",
                    user if user and not user.is_anonymous else None,
                )
        # NOTE: intentionally NO terminal_close to the agent — a transient
        # browser disconnect (tab switch, iOS backgrounding) must not kill
        # the shell. The shell persists for reattach; explicit user close
        # (receive type=close) is the only path that tears it down.
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
```

- [ ] **Step 4: Add `restart` to `receive()`**

In `TerminalConsumer.receive()`, add a branch (after the `resize` branch, before `close`):
```python
        elif msg_type == "restart":
            await self.channel_layer.group_send(
                f"{self.group_name}_agent", {"type": "terminal_restart"}
            )
```

- [ ] **Step 5: Add agent-side handlers in `AgentTerminalConsumer`**

Add to `AgentTerminalConsumer` (next to `terminal_input`/`terminal_resize`/`terminal_close`):
```python
    async def terminal_ensure(self, event):
        """Browser (re)connected -> tell the agent to guarantee a live shell."""
        await self.send(text_data=json.dumps({"type": "ensure"}))

    async def terminal_restart(self, event):
        """Browser requested a shell restart -> forward to the agent."""
        await self.send(text_data=json.dumps({"type": "restart"}))
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `pytest tests/test_terminal_consumer.py -v`
Expected: PASS (all, including Task 3 tests).

- [ ] **Step 7: Commit**

```bash
git add apps/tunnel/consumers.py tests/test_terminal_consumer.py
git commit -m "feat(tunnel): persist shell on transient disconnect; forward restart/ensure"
```

---

## Task 5: Frontend — restart button, reject-reason, auto-reconnect

**Files:**
- Modify: `static/js/app.js` (the `initTerminal` function, lines ~287-327)
- Modify: `apps/stations/templates/stations/station_detail.html` (terminal panel, near line ~389)

**REQUIRED SUB-SKILL for the implementer:** Use `Skill("frontend-design")` for the button styling/placement (project rule: all UI work goes through frontend-design).

**Interfaces:**
- Consumes: server messages `{type:"output"}`, `{type:"closed",reason}`, `{type:"error",reason,code}`; sends `{type:"input"}`, `{type:"resize"}`, `{type:"restart"}`.
- Produces: a `#xterm-restart` button; a reconnecting terminal client with capped backoff.

- [ ] **Step 1: Add the Restart button to the template**

In `apps/stations/templates/stations/station_detail.html`, in the terminal panel header near the `#xterm-container` (line ~389), add (style per frontend-design skill):
```html
<button type="button" id="xterm-restart" class="btn btn-sm btn-outline-warning" title="{% translate 'Restart shell' %}">
  {% translate 'Restart shell' %}
</button>
```
(Ensure `{% load i18n %}` is present at top of the template; use `{% comment %}` if any comment is needed, never multi-line `{# #}`.)

- [ ] **Step 2: Rewrite `initTerminal` in `static/js/app.js` for reconnect + reject-reason + restart**

Replace the `initTerminal` function body (lines ~287-327) with:
```javascript
  function initTerminal() {
    var host = document.getElementById("xterm-container");
    if (!host || typeof Terminal === "undefined") return;
    var stationId = host.getAttribute("data-station-id");
    if (!stationId) return;

    var term = new Terminal({
      cursorBlink: true,
      fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
      fontSize: 13,
      theme: { background: "#000000", foreground: "#F5F7FA", cursor: "#FF8A3D",
               selection: "rgba(255, 138, 61, 0.3)" },
    });
    term.open(host);

    var ws = null;
    var userClosed = false;
    var backoff = 1000;
    var BACKOFF_MAX = 15000;
    var MAX_ATTEMPTS = 8;
    var attempts = 0;

    function color(s, c) { return "\x1b[" + c + "m" + s + "\x1b[0m"; }

    function connect() {
      var proto = location.protocol === "https:" ? "wss:" : "ws:";
      term.write(color("Connecting to station #" + stationId + "...", "90") + "\r\n");
      ws = new WebSocket(proto + "//" + location.host + "/ws/terminal/" + stationId + "/");

      ws.addEventListener("open", function () {
        attempts = 0; backoff = 1000;
        term.write(color("[ connected ]", "32") + "\r\n");
      });
      ws.addEventListener("message", function (ev) {
        try {
          var payload = JSON.parse(ev.data);
          if (payload.type === "output") term.write(payload.data);
          else if (payload.type === "closed")
            term.write("\r\n" + color("[ closed: " + (payload.reason || "") + " ]", "31"));
          else if (payload.type === "error") {
            userClosed = true;  // an operational reject is not worth retrying
            term.write("\r\n" + color("[ " + (payload.reason || "rejected") + " ]", "31"));
          }
        } catch (_) {}
      });
      ws.addEventListener("close", function (ev) {
        if (userClosed) {
          term.write("\r\n" + color("[ disconnected: " + ev.code + " ]", "33"));
          return;
        }
        attempts += 1;
        if (attempts > MAX_ATTEMPTS) {
          term.write("\r\n" + color("[ disconnected — giving up after " + MAX_ATTEMPTS +
            " attempts; refresh to retry ]", "31"));
          return;
        }
        term.write("\r\n" + color("[ reconnecting… (" + attempts + ") ]", "33") + "\r\n");
        setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, BACKOFF_MAX);
      });
    }

    term.onData(function (data) {
      if (ws && ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "input", data: data }));
    });

    var restartBtn = document.getElementById("xterm-restart");
    if (restartBtn) {
      restartBtn.addEventListener("click", function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
          term.write("\r\n" + color("[ restarting shell… ]", "33") + "\r\n");
          ws.send(JSON.stringify({ type: "restart" }));
        }
      });
    }

    connect();
  }
```

- [ ] **Step 3: Manual verification (documented; no JS test harness in repo)**

Run the app locally (`python manage.py runserver`) with an admin user and a station marked online plus a connected agent, then:
- Confirm the terminal shows `[ connected ]` and a prompt.
- Click **Restart shell** → shell restarts, fresh prompt.
- Kill the browser WS (devtools → offline briefly) → `[ reconnecting… ]` → recovers to a working shell.
- As a non-admin user → `[ Terminal access is restricted to admins ]` instead of a bare `1006`.

Record the observations in the PR description (evidence per verification-before-completion).

- [ ] **Step 4: Commit**

```bash
git add static/js/app.js apps/stations/templates/stations/station_detail.html
git commit -m "feat(frontend): terminal restart button, reject reasons, auto-reconnect"
```

---

## Task 6: Integration verification (probe / E2E)

**Files:** none (verification task).

- [ ] **Step 1: Run the full server test suite**

Run: `pytest tests/test_terminal_consumer.py tests/test_terminal_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 2: Full-suite regression**

Run: `pytest`
Expected: PASS (no regressions elsewhere).

- [ ] **Step 3: Lint/format**

Run: `ruff format --check . && ruff check .`
Expected: clean. (Fix + re-run if needed.)

- [ ] **Step 4: E2E data-path check (integration-tester scope)**

With an admin browser + agent connected to a test station: type a command → verify it reaches the shell (`echo hi` → `hi`), resize works, restart yields a fresh shell, transient disconnect reattaches to the same shell, and a killed shell auto-respawns on reconnect. Record evidence in the PR.

---

## Self-Review (completed by plan author)

**Spec coverage:**
- Model C persist/reattach → Tasks 1 (agent ensure), 4 (no shell-kill on disconnect). ✓
- Auto-respawn on connect if dead → Task 3 (`terminal_ensure` on connect) + Task 1 (`_ensure_shell`). ✓
- Manual restart → Tasks 4 (receive `restart` → `terminal_restart`), 1 (`_restart_shell`), 5 (button). ✓
- Reject-reason visibility (#1) → Task 3 (accept-then-error `_reject`) + Task 5 (render `error`). ✓
- Auto-reconnect (#2) → Task 5. ✓
- Admin gate (#3) → Task 3 (`user.is_admin`). ✓
- Session staleness → Tasks 2 (`last_seen`), 3 (keepalive, reap, stale-aware count, create-after-accept). ✓
- Testing → Tasks 1,3,4 (unit), 5 (manual), 6 (E2E). ✓
- Deployment/OTA sequencing → server+frontend Tasks 2-5 ship via image; agent Task 1 via SRCREV/OTA (handled outside this repo). ✓

**Placeholder scan:** none — every code step contains full code.

**Type consistency:** `_ensure_shell`/`_restart_shell`/`_shell_alive`/`_cancel_reader`/`_reader_task` (Task 1) consistent; `_reject`/`_reap_stale_sessions`/`_touch_session`/`_keepalive_loop`/`keepalive_task`/`last_seen` consistent across Tasks 2-4; message types `terminal_ensure`/`terminal_restart`/`ensure`/`restart`/`error` consistent server↔agent↔frontend.

**Note on ordering:** Task 1 (agent) is independent and OTA-shipped; Tasks 2→3→4 are sequential (shared file/connect logic); Task 5 depends on Task 3's `error` protocol; Task 6 last.
