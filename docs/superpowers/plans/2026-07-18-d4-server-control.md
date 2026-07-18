# D4 — Server: StationModule-Registry + Control-Relay + TX-Lock — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the server counterpart of the D3 agent↔server control contract: two WebSocket consumers bridged over a channel group, a persistent StationModule registry, a per-station TX-lock with full hand-off, and access-control — so a browser can command a module (relayed to the agent), see live/last state, and hand control off safely.

**Architecture:** A new `apps.control` Django app owns the whole D4 concern (registry model, lock model + logic, both consumers, routing, config). It mirrors the proven `apps.tunnel` terminal-relay pattern: an **agent-facing** `AgentControlConsumer` (Ed25519-authenticated, exactly one per station) and a **browser-facing** `ControlConsumer` (Django-session access-controlled), bridged over the channel-layer group `control_<station_id>` (+ an `_agent` sub-group for server→agent frames). The server **relays the D3 §7 contract verbatim** — it only *gates* (access + lock), *caches* (registry), and *pushes*. All shared authoritative state (registry rows, lock ownership) lives in the **database**, because production runs multiple ASGI workers over a Redis channel layer; in-memory per-process state would be inconsistent. Timers (T_idle auto-free, reconnect-grace) are driven by pure, directly-testable sweep functions plus a periodic caller on the single per-station agent consumer.

**Tech Stack:** Django 6.0, Django Channels (`AsyncWebsocketConsumer`, channel-layer groups), `channels.db.database_sync_to_async`, PostgreSQL/JSONField, `channels.testing.WebsocketCommunicator`, pytest (NO pytest-asyncio — async scenarios run via `asyncio.run()` inside `@pytest.mark.django_db(transaction=True)` sync tests).

## Global Constraints

- **Relay verbatim, never transform the §7 contract.** Message envelope is `{"v": 1, "type": …, …}`. Agent→Server types: `inventory`/`state`/`result`/`event`. Server→Agent types: `command`/`subscribe`/`unsubscribe`/`ptt_keepalive`. The server MUST NOT rewrite field names or restructure payloads that pass through.
- **DB-backed authoritative state.** Lock ownership and registry rows live in the DB (prod = multi-worker Redis channel layer). Never keep lock/registry authority in a module-level dict.
- **Soft, never hard.** Modules not reported in an `inventory` → `online=False` (never deleted) — consistent with the ImageRelease soft-delete pattern and FK safety.
- **Persist descriptors + `last_state` (settings) only. Telemetry (e.g. `rssi`) is ephemeral — NEVER persisted**, only broadcast live. Settings-vs-telemetry is decided by the capability's `kind` in the `capability_descriptor`.
- **Access mapping:** See+control (acquire lock, commands, PTT) → `user.can_use_station(station)`. Forced preemption/config → `user.is_station_admin(station)` / `user.can_administer_station(station)` / `user.is_admin`. Applicants are always excluded (`can_use_station` already returns False for them).
- **The lock is USER-owned**, not WS-owned: multiple tabs of the same user share the hold.
- **Ed25519 agent auth** reuses the exact scheme from `AgentTerminalConsumer._verify_agent` (query `signature`+`timestamp`, 60 s replay window, `DeviceKey.verify_signature` against current + next public key, body-hash of `b""`).
- **Accept-then-error reject** for browser rejects (mirror `TerminalConsumer._reject`): `await self.accept()` first, then send `{type:"error", reason, code}` and `close(code)` — so the browser sees a real reason, not an opaque 1006.
- **Django template comment rule** (if any template is touched): only `{% comment %}…{% endcomment %}`, never multi-line `{# … #}`. (D4 is backend-only; no templates expected.)
- **Multi-line-comment / number-input locale rules** from CLAUDE.md apply if UI is ever touched (it is not in D4).

---

## Design decisions locked in this plan

1. **New app `apps.control`.** Files that change together live together: registry model, lock, both consumers, routing, config. FK to `stations.Station`. Registered in `INSTALLED_APPS` after `apps.tunnel`.
2. **Two channel groups per station** (mirrors tunnel): `control_<id>` (viewers + broadcasts) and `control_<id>_agent` (server→agent frames). The `AgentControlConsumer` joins both; `ControlConsumer` joins only `control_<id>`.
3. **`ControlLock` is a DB model** keyed unique `(station, scope)` with `scope="station"` today (erweiterbar). Lock ops are `@transaction.atomic` + `select_for_update`. Timers are timestamp fields (`last_activity`, `pending_release_at`) swept by a pure function.
4. **Gating split** (documented decision; the ticket text bundles `subscribe` under "gated durch den Lock", but the whole D5 multi-viewer design needs read-only viewers to see telemetry):
   - `command` and `ptt_keepalive` → **lock-holder only** (+ `can_use_station`).
   - `subscribe`/`unsubscribe` → **`can_use_station` only** (any viewer may watch telemetry; a subscription is a server↔agent stream request, not radio control).
   This is called out explicitly in the PR description for reviewer sign-off.
5. **Command timeout** is tracked per originating `ControlConsumer` instance (the browser that sent it): schedule an `asyncio` timer keyed by `request_id`; the broadcast `result` (which reaches the originator too) cancels it; otherwise a structured `{type:"error", request_id, error:{code:"timeout"}}` is pushed to that browser.
6. **Lock sweep** (T_idle + reconnect-grace) runs as a periodic loop on the **single** `AgentControlConsumer` per station (there is exactly one agent WS), plus the same pure `sweep_lock()` function is called opportunistically and directly in tests. When the agent disconnects, the lock is freed anyway, so having the agent own the timer is correct.

## File structure

- `apps/control/__init__.py` — empty package marker.
- `apps/control/apps.py` — `ControlConfig` AppConfig.
- `apps/control/models.py` — `StationModule` (registry) + `ControlLock` (lock state).
- `apps/control/migrations/0001_initial.py` — auto-generated.
- `apps/control/lock.py` — pure sync lock operations + `sweep_lock()`.
- `apps/control/registry.py` — pure sync registry operations (inventory upsert, state merge, offline).
- `apps/control/consumers.py` — `AgentControlConsumer` + `ControlConsumer`.
- `apps/control/routing.py` — `websocket_urlpatterns` + `agent_websocket_urlpatterns`.
- `apps/control/admin.py` — read-only-ish admin for `StationModule` + `ControlLock`.
- `apps/control/constants.py` — config accessors with settings overrides + defaults.
- `config/asgi.py` — wire control routing into browser + agent routers (modify).
- `config/settings/base.py` — control config defaults (modify).
- `apps/stations/models.py` — add `StationAuditLog.EventType` members for control events (modify) + migration.
- `tests/test_control_registry.py` — registry unit tests (sync).
- `tests/test_control_lock.py` — lock unit tests (sync).
- `tests/test_control_consumer_relay.py` — relay/registry-via-WS/edge Channels tests.
- `tests/test_control_consumer_lock.py` — lock/access/hand-off Channels tests.

---

## Task 1: App scaffold + `StationModule` registry model

**Files:**
- Create: `apps/control/__init__.py`, `apps/control/apps.py`, `apps/control/models.py`
- Modify: `config/settings/base.py` (add `"apps.control"` to `INSTALLED_APPS`)
- Test: `tests/test_control_registry.py`

**Interfaces:**
- Produces: `apps.control.models.StationModule` with fields `station` (FK→`stations.Station`, CASCADE, `related_name="modules"`), `slot` (Char), `module_id` (Char), `type`/`model`/`version` (Char), `capability_descriptor` (JSON, default `list`), `last_state` (JSON, default `dict`), `online` (bool), `last_seen` (DateTime null). Unique `(station, slot, module_id)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_registry.py
import pytest
from django.db import IntegrityError

from apps.stations.models import Station


@pytest.mark.django_db
def test_stationmodule_unique_slot_module():
    from apps.control.models import StationModule

    station = Station.objects.create(name="s1")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0")
    with pytest.raises(IntegrityError):
        StationModule.objects.create(station=station, slot="slot0", module_id="fm0")


@pytest.mark.django_db
def test_stationmodule_defaults():
    from apps.control.models import StationModule

    station = Station.objects.create(name="s2")
    m = StationModule.objects.create(station=station, slot="slot1", module_id="fm0")
    assert m.online is False
    assert m.last_state == {}
    assert m.capability_descriptor == []
    assert m.last_seen is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_control_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.control'`.

- [ ] **Step 3: Create the app scaffold + model**

```python
# apps/control/__init__.py
```
(empty file)

```python
# apps/control/apps.py
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ControlConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.control"
    verbose_name = _("Control")
```

```python
# apps/control/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _


class StationModule(models.Model):
    """A module discovered on a station via the agent's ``inventory`` snapshot.

    Descriptor + last settings state are persisted so the UI can render the
    panel even while the station is offline. Telemetry is never stored here.
    """

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="modules",
    )
    slot = models.CharField(_("slot"), max_length=64)
    module_id = models.CharField(_("module id"), max_length=128)

    # Identity (from inventory ``identity``).
    type = models.CharField(_("type"), max_length=128, blank=True)
    model = models.CharField(_("model"), max_length=128, blank=True)
    version = models.CharField(_("version"), max_length=64, blank=True)

    capability_descriptor = models.JSONField(_("capability descriptor"), default=list, blank=True)
    last_state = models.JSONField(_("last state"), default=dict, blank=True)

    online = models.BooleanField(_("online"), default=False)
    last_seen = models.DateTimeField(_("last seen"), null=True, blank=True)

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("station module")
        verbose_name_plural = _("station modules")
        ordering = ["station", "slot", "module_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["station", "slot", "module_id"],
                name="uniq_station_slot_module",
            ),
        ]
        indexes = [
            models.Index(fields=["station", "online"]),
        ]

    def __str__(self):
        return f"{self.station_id}/{self.slot}/{self.module_id}"
```

Add `"apps.control",` to `INSTALLED_APPS` in `config/settings/base.py` immediately after `"apps.tunnel",`.

- [ ] **Step 4: Make the migration**

Run: `python manage.py makemigrations control`
Expected: creates `apps/control/migrations/0001_initial.py` with `StationModule`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_control_registry.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add apps/control/__init__.py apps/control/apps.py apps/control/models.py apps/control/migrations/ config/settings/base.py tests/test_control_registry.py
git commit -m "feat(control): scaffold apps.control + StationModule registry model"
```

---

## Task 2: Registry operations (inventory upsert, state merge, offline)

**Files:**
- Create: `apps/control/registry.py`
- Test: `tests/test_control_registry.py` (extend)

**Interfaces:**
- Consumes: `StationModule` (Task 1).
- Produces (all pure sync, no I/O):
  - `apply_inventory(station, slots: list) -> None` — upsert every reported module (`online=True`, `last_seen=now`), set every other module of the station `online=False`. `slots` is the §7 `inventory.slots`: `[{"slot", "modules":[{"module","identity":{...},"capabilities":[...],"state":{cap:val}}]}]`.
  - `apply_state(station, slot, module_id, values: dict) -> None` — merge only **setting** caps of `values` into that module's `last_state` (telemetry caps skipped, decided by `kind` in `capability_descriptor`). No-op if the module is unknown.
  - `mark_station_offline(station) -> None` — set every module of the station `online=False`.
  - `is_setting_cap(descriptor: list, cap_name: str) -> bool` — helper: True iff the cap's `kind == "setting"` in the descriptor (unknown cap → False, so unknown caps are treated as ephemeral and not persisted).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_control_registry.py  (append)
from django.utils import timezone


def _fm_descriptor():
    return [
        {"name": "frequency", "kind": "setting", "type": "float"},
        {"name": "rssi", "kind": "telemetry", "type": "int"},
    ]


@pytest.mark.django_db
def test_apply_inventory_upserts_and_marks_offline():
    from apps.control import registry
    from apps.control.models import StationModule

    station = Station.objects.create(name="inv1")
    # A previously-known module that will NOT be in the new inventory.
    stale = StationModule.objects.create(
        station=station, slot="slot9", module_id="old0", online=True
    )

    slots = [
        {
            "slot": "slot0",
            "modules": [
                {
                    "module": "fm0",
                    "identity": {"type": "fm", "model": "SA818", "version": "1.2"},
                    "capabilities": _fm_descriptor(),
                    "state": {"frequency": 145.5},
                }
            ],
        }
    ]
    registry.apply_inventory(station, slots)

    m = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert m.online is True
    assert m.type == "fm" and m.model == "SA818" and m.version == "1.2"
    assert m.capability_descriptor == _fm_descriptor()
    assert m.last_state == {"frequency": 145.5}
    assert m.last_seen is not None

    stale.refresh_from_db()
    assert stale.online is False  # soft — still present


@pytest.mark.django_db
def test_apply_state_persists_settings_not_telemetry():
    from apps.control import registry
    from apps.control.models import StationModule

    station = Station.objects.create(name="st1")
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm0",
        capability_descriptor=_fm_descriptor(),
        last_state={"frequency": 145.5},
    )
    registry.apply_state(station, "slot0", "fm0", {"frequency": 146.0, "rssi": -70})

    m = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert m.last_state == {"frequency": 146.0}  # rssi (telemetry) NOT persisted


@pytest.mark.django_db
def test_apply_state_unknown_module_is_noop():
    from apps.control import registry

    station = Station.objects.create(name="st2")
    registry.apply_state(station, "slotX", "nope", {"frequency": 1.0})  # must not raise


@pytest.mark.django_db
def test_mark_station_offline():
    from apps.control import registry
    from apps.control.models import StationModule

    station = Station.objects.create(name="off1")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)
    registry.mark_station_offline(station)
    assert StationModule.objects.filter(station=station, online=True).count() == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_control_registry.py -k "apply or offline" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.control.registry'`.

- [ ] **Step 3: Implement `registry.py`**

```python
# apps/control/registry.py
"""Pure, synchronous registry operations for StationModule.

No async / no I/O beyond the ORM — call from consumers via
``database_sync_to_async`` and directly from unit tests.
"""

from django.db import transaction
from django.utils import timezone

from .models import StationModule


def is_setting_cap(descriptor, cap_name):
    """True iff ``cap_name`` is a *setting* capability in ``descriptor``.

    Unknown caps return False so they are treated as ephemeral (telemetry)
    and never persisted into ``last_state``.
    """
    for cap in descriptor or []:
        if cap.get("name") == cap_name:
            return cap.get("kind") == "setting"
    return False


@transaction.atomic
def apply_inventory(station, slots):
    """Upsert all reported modules; soft-offline every module not reported."""
    now = timezone.now()
    reported = []
    for slot_entry in slots or []:
        slot = slot_entry.get("slot")
        for mod in slot_entry.get("modules", []) or []:
            module_id = mod.get("module")
            if slot is None or module_id is None:
                continue
            identity = mod.get("identity") or {}
            StationModule.objects.update_or_create(
                station=station,
                slot=slot,
                module_id=module_id,
                defaults={
                    "type": identity.get("type", ""),
                    "model": identity.get("model", ""),
                    "version": identity.get("version", ""),
                    "capability_descriptor": mod.get("capabilities", []) or [],
                    "last_state": mod.get("state", {}) or {},
                    "online": True,
                    "last_seen": now,
                },
            )
            reported.append((slot, module_id))

    qs = StationModule.objects.filter(station=station, online=True)
    for slot, module_id in reported:
        qs = qs.exclude(slot=slot, module_id=module_id)
    qs.update(online=False)


@transaction.atomic
def apply_state(station, slot, module_id, values):
    """Merge only *setting* caps of ``values`` into the module's last_state."""
    try:
        module = StationModule.objects.select_for_update().get(
            station=station, slot=slot, module_id=module_id
        )
    except StationModule.DoesNotExist:
        return
    descriptor = module.capability_descriptor
    changed = False
    for cap_name, value in (values or {}).items():
        if is_setting_cap(descriptor, cap_name):
            module.last_state[cap_name] = value
            changed = True
    if changed:
        module.save(update_fields=["last_state", "updated_at"])


def mark_station_offline(station):
    StationModule.objects.filter(station=station).update(online=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_control_registry.py -v`
Expected: PASS (all registry tests).

- [ ] **Step 5: Commit**

```bash
git add apps/control/registry.py tests/test_control_registry.py
git commit -m "feat(control): registry upsert/state-merge/offline operations"
```

---

## Task 3: `ControlLock` model + lock operations + sweep

**Files:**
- Modify: `apps/control/models.py` (add `ControlLock`)
- Create: `apps/control/lock.py`, `apps/control/constants.py`
- Modify: `config/settings/base.py` (control config defaults)
- Test: `tests/test_control_lock.py`

**Interfaces:**
- Produces `apps.control.models.ControlLock`: `station` (FK→Station, CASCADE, `related_name="control_locks"`), `scope` (Char, default `"station"`), `holder` (FK→User, null, SET_NULL), `acquired_at` (DateTime null), `last_activity` (DateTime null), `pending_release_at` (DateTime null). Unique `(station, scope)`.
- Produces `apps.control.lock` pure sync ops, each returning a small result dict/bool for the caller to broadcast:
  - `get_or_create_lock(station, scope="station") -> ControlLock`
  - `acquire(station, user, scope="station") -> bool` — True iff it was FREE (or already held by `user`) and is now held by `user`.
  - `release(station, user, scope="station") -> bool` — True iff `user` was holder and it is now FREE.
  - `request_control(station, user, scope="station") -> ControlLock | None` — returns the current lock if held by someone else (so caller can notify the holder); None if FREE/self.
  - `transfer(station, from_user, to_user_id, scope="station") -> bool` — True iff `from_user` was holder and hold moved to `to_user_id`.
  - `preempt(station, user, scope="station") -> bool` — force `user` to holder regardless of prior state (caller must have already checked admin rights).
  - `touch(station, user, scope="station") -> bool` — update `last_activity` iff `user` is holder; returns holder-ness.
  - `holder_disconnected(station, user, grace_seconds, scope="station") -> None` — if `user` is holder, set `pending_release_at = now + grace`.
  - `holder_reconnected(station, user, scope="station") -> None` — if `user` is holder, clear `pending_release_at`.
  - `sweep_lock(station, now, idle_seconds, scope="station") -> bool` — free the lock if (`pending_release_at` set and `now >= pending_release_at`) OR (`last_activity` set and `now - last_activity > idle_seconds`). Returns True iff it freed a previously-held lock.
  - `lock_status(lock) -> dict` — `{"state": "free"|"held", "holder_id": int|None, "holder_username": str|None, "since": iso|None}`.
- Produces `apps.control.constants`: `T_IDLE_SECONDS`, `RECONNECT_GRACE_SECONDS`, `MAX_VIEWERS_PER_STATION`, `COMMAND_TIMEOUT_SECONDS`, `LOCK_SWEEP_INTERVAL_SECONDS` (module-level, read from settings with defaults).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_control_lock.py
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.stations.models import Station


def _user(name):
    return User.objects.create(username=name, membership_level=User.MembershipLevel.MEMBER)


@pytest.mark.django_db
def test_acquire_from_free_then_second_user_blocked():
    from apps.control import lock

    station = Station.objects.create(name="l1")
    a, b = _user("a"), _user("b")

    assert lock.acquire(station, a) is True
    assert lock.acquire(station, b) is False  # already held by a
    assert lock.acquire(station, a) is True  # idempotent for same holder


@pytest.mark.django_db
def test_release_only_by_holder():
    from apps.control import lock

    station = Station.objects.create(name="l2")
    a, b = _user("a2"), _user("b2")
    lock.acquire(station, a)
    assert lock.release(station, b) is False
    assert lock.release(station, a) is True
    assert lock.acquire(station, b) is True  # now free -> b can take it


@pytest.mark.django_db
def test_targeted_transfer():
    from apps.control import lock

    station = Station.objects.create(name="l3")
    a, b = _user("a3"), _user("b3")
    lock.acquire(station, a)
    # request just reports the current holder
    assert lock.request_control(station, b).holder_id == a.id
    assert lock.transfer(station, a, b.id) is True
    assert lock.acquire(station, a) is False  # b holds it now
    assert lock.release(station, b) is True


@pytest.mark.django_db
def test_transfer_rejected_when_not_holder():
    from apps.control import lock

    station = Station.objects.create(name="l4")
    a, b, c = _user("a4"), _user("b4"), _user("c4")
    lock.acquire(station, a)
    assert lock.transfer(station, b, c.id) is False  # b is not the holder


@pytest.mark.django_db
def test_preempt_forces_holder():
    from apps.control import lock

    station = Station.objects.create(name="l5")
    a, admin = _user("a5"), _user("admin5")
    lock.acquire(station, a)
    assert lock.preempt(station, admin) is True
    assert lock.acquire(station, a) is False  # admin holds it


@pytest.mark.django_db
def test_sweep_idle_frees_lock():
    from apps.control import lock

    station = Station.objects.create(name="l6")
    a = _user("a6")
    lock.acquire(station, a)
    lock.touch(station, a)
    now = timezone.now() + timedelta(seconds=600)  # 10 min later
    assert lock.sweep_lock(station, now=now, idle_seconds=120) is True
    assert lock.acquire(station, _user("z6")) is True  # was freed


@pytest.mark.django_db
def test_sweep_reconnect_grace():
    from apps.control import lock

    station = Station.objects.create(name="l7")
    a = _user("a7")
    lock.acquire(station, a)
    lock.holder_disconnected(station, a, grace_seconds=12)
    # Before the grace deadline: still held.
    soon = timezone.now() + timedelta(seconds=5)
    assert lock.sweep_lock(station, now=soon, idle_seconds=999999) is False
    # Reconnect clears the pending release.
    lock.holder_reconnected(station, a)
    later = timezone.now() + timedelta(seconds=30)
    assert lock.sweep_lock(station, now=later, idle_seconds=999999) is False
    # Disconnect again, let the grace lapse -> freed.
    lock.holder_disconnected(station, a, grace_seconds=12)
    past_grace = timezone.now() + timedelta(seconds=30)
    assert lock.sweep_lock(station, now=past_grace, idle_seconds=999999) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_control_lock.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.control.lock'`.

- [ ] **Step 3: Add `ControlLock` model**

Append to `apps/control/models.py`:

```python
class ControlLock(models.Model):
    """Per-(station, scope) TX-lock. USER-owned (shared across the user's tabs).

    ``scope`` is ``"station"`` today; the unique key leaves room to extend to
    per-module or role scopes later without a schema change to the holder logic.
    """

    station = models.ForeignKey(
        "stations.Station",
        verbose_name=_("station"),
        on_delete=models.CASCADE,
        related_name="control_locks",
    )
    scope = models.CharField(_("scope"), max_length=64, default="station")
    holder = models.ForeignKey(
        "accounts.User",
        verbose_name=_("holder"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="held_control_locks",
    )
    acquired_at = models.DateTimeField(_("acquired at"), null=True, blank=True)
    last_activity = models.DateTimeField(_("last activity"), null=True, blank=True)
    pending_release_at = models.DateTimeField(_("pending release at"), null=True, blank=True)

    class Meta:
        verbose_name = _("control lock")
        verbose_name_plural = _("control locks")
        constraints = [
            models.UniqueConstraint(fields=["station", "scope"], name="uniq_station_scope_lock"),
        ]

    def __str__(self):
        who = self.holder_id or "FREE"
        return f"lock({self.station_id}/{self.scope})={who}"
```

Run: `python manage.py makemigrations control` → adds `ControlLock` to a new migration (e.g. `0002_controllock.py`).

- [ ] **Step 4: Implement `constants.py`**

```python
# apps/control/constants.py
from django.conf import settings

T_IDLE_SECONDS = getattr(settings, "CONTROL_T_IDLE_SECONDS", 300)
RECONNECT_GRACE_SECONDS = getattr(settings, "CONTROL_RECONNECT_GRACE_SECONDS", 12)
MAX_VIEWERS_PER_STATION = getattr(settings, "CONTROL_MAX_VIEWERS_PER_STATION", 8)
COMMAND_TIMEOUT_SECONDS = getattr(settings, "CONTROL_COMMAND_TIMEOUT_SECONDS", 10)
LOCK_SWEEP_INTERVAL_SECONDS = getattr(settings, "CONTROL_LOCK_SWEEP_INTERVAL_SECONDS", 5)
```

Add to `config/settings/base.py` (near the Channels block):

```python
# Control-plane (D4) tunables.
CONTROL_T_IDLE_SECONDS = 300           # idle lock auto-free (5 min)
CONTROL_RECONNECT_GRACE_SECONDS = 12   # hold survives a short WS blip
CONTROL_MAX_VIEWERS_PER_STATION = 8    # analog MAX_SESSIONS_PER_STATION
CONTROL_COMMAND_TIMEOUT_SECONDS = 10   # no result -> timeout error to browser
CONTROL_LOCK_SWEEP_INTERVAL_SECONDS = 5
```

- [ ] **Step 5: Implement `lock.py`**

```python
# apps/control/lock.py
"""Pure, synchronous TX-lock operations over ControlLock rows.

Every mutation is atomic + row-locked so concurrent workers can't both
acquire. Callers (consumers) wrap these in database_sync_to_async and
broadcast lock_status() afterwards.
"""

from django.db import transaction
from django.utils import timezone

from .models import ControlLock


def get_or_create_lock(station, scope="station"):
    lock, _ = ControlLock.objects.get_or_create(station=station, scope=scope)
    return lock


def _locked(station, scope):
    return ControlLock.objects.select_for_update().get_or_create(station=station, scope=scope)[0]


@transaction.atomic
def acquire(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id in (None, user.id):
        now = timezone.now()
        if lock.holder_id is None:
            lock.acquired_at = now
        lock.holder = user
        lock.last_activity = now
        lock.pending_release_at = None
        lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])
        return True
    return False


@transaction.atomic
def release(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id == user.id:
        _clear(lock)
        return True
    return False


@transaction.atomic
def request_control(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id is not None and lock.holder_id != user.id:
        return lock
    return None


@transaction.atomic
def transfer(station, from_user, to_user_id, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id != from_user.id:
        return False
    now = timezone.now()
    lock.holder_id = to_user_id
    lock.acquired_at = now
    lock.last_activity = now
    lock.pending_release_at = None
    lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])
    return True


@transaction.atomic
def preempt(station, user, scope="station"):
    lock = _locked(station, scope)
    now = timezone.now()
    lock.holder = user
    lock.acquired_at = now
    lock.last_activity = now
    lock.pending_release_at = None
    lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])
    return True


@transaction.atomic
def touch(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id == user.id:
        lock.last_activity = timezone.now()
        lock.save(update_fields=["last_activity"])
        return True
    return False


@transaction.atomic
def holder_disconnected(station, user, grace_seconds, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id == user.id:
        lock.pending_release_at = timezone.now() + timezone.timedelta(seconds=grace_seconds)
        lock.save(update_fields=["pending_release_at"])


@transaction.atomic
def holder_reconnected(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id == user.id and lock.pending_release_at is not None:
        lock.pending_release_at = None
        lock.save(update_fields=["pending_release_at"])


@transaction.atomic
def sweep_lock(station, now, idle_seconds, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id is None:
        return False
    grace_lapsed = lock.pending_release_at is not None and now >= lock.pending_release_at
    idle_lapsed = (
        lock.last_activity is not None
        and (now - lock.last_activity).total_seconds() > idle_seconds
    )
    if grace_lapsed or idle_lapsed:
        _clear(lock)
        return True
    return False


def _clear(lock):
    lock.holder = None
    lock.acquired_at = None
    lock.last_activity = None
    lock.pending_release_at = None
    lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])


def lock_status(lock):
    if lock.holder_id is None:
        return {"state": "free", "holder_id": None, "holder_username": None, "since": None}
    return {
        "state": "held",
        "holder_id": lock.holder_id,
        "holder_username": lock.holder.username if lock.holder else None,
        "since": lock.acquired_at.isoformat() if lock.acquired_at else None,
    }
```

Note: `timezone.timedelta` is not an attribute — replace with `from datetime import timedelta` at the top of `lock.py` and use `timedelta(seconds=grace_seconds)`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_control_lock.py -v`
Expected: PASS (all lock tests).

- [ ] **Step 7: Commit**

```bash
git add apps/control/models.py apps/control/lock.py apps/control/constants.py apps/control/migrations/ config/settings/base.py tests/test_control_lock.py
git commit -m "feat(control): ControlLock model + lock ops (acquire/transfer/preempt/sweep) + config"
```

---

## Task 4: Routing + ASGI wiring + admin

**Files:**
- Create: `apps/control/routing.py`, `apps/control/admin.py`
- Modify: `config/asgi.py`
- Test: (covered by Task 5/6 WS tests; this task adds no standalone test — it is scaffolding folded into the consumer tasks. Verify with a manual import check.)

**Interfaces:**
- Produces `apps.control.routing.websocket_urlpatterns` (browser) and `agent_websocket_urlpatterns` (agent), referencing `consumers.ControlConsumer` and `consumers.AgentControlConsumer` (Task 5/6).

- [ ] **Step 1: Create `routing.py`**

```python
# apps/control/routing.py
from django.urls import re_path

from . import consumers

# Browser-side (Django session auth via AllowedHostsOriginValidator stack).
websocket_urlpatterns = [
    re_path(r"ws/control/(?P<station_id>\d+)/$", consumers.ControlConsumer.as_asgi()),
]

# Agent-side (Ed25519 query-param auth; skips origin validation).
agent_websocket_urlpatterns = [
    re_path(
        r"ws/agent/control/(?P<station_id>\d+)/$",
        consumers.AgentControlConsumer.as_asgi(),
    ),
]
```

- [ ] **Step 2: Create `admin.py`**

```python
# apps/control/admin.py
from django.contrib import admin

from .models import ControlLock, StationModule


@admin.register(StationModule)
class StationModuleAdmin(admin.ModelAdmin):
    list_display = ("station", "slot", "module_id", "type", "online", "last_seen")
    list_filter = ("online", "type")
    search_fields = ("module_id", "type", "model")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ControlLock)
class ControlLockAdmin(admin.ModelAdmin):
    list_display = ("station", "scope", "holder", "acquired_at", "last_activity")
    list_filter = ("scope",)
```

- [ ] **Step 3: Wire into `config/asgi.py`**

Modify the imports and route lists:

```python
from apps.control import routing as control_routing  # noqa: E402
from apps.deployments import routing as deployments_routing  # noqa: E402
from apps.stations import routing as stations_routing  # noqa: E402
from apps.tunnel import routing as tunnel_routing  # noqa: E402

browser_ws_routes = (
    stations_routing.websocket_urlpatterns
    + deployments_routing.websocket_urlpatterns
    + tunnel_routing.websocket_urlpatterns
    + control_routing.websocket_urlpatterns
)

agent_ws_routes = (
    tunnel_routing.agent_websocket_urlpatterns
    + control_routing.agent_websocket_urlpatterns
)
```

- [ ] **Step 4: Verify import wiring (deferred until consumers exist)**

This task's `routing.py` imports `consumers`, which is created in Task 5. Implement Task 5 next; then:

Run: `python -c "import django; django.setup(); import config.asgi"` (with `DJANGO_SETTINGS_MODULE=config.settings.test`)
Expected: no ImportError.

- [ ] **Step 5: Commit (after Task 5 so imports resolve)**

Fold this commit into Task 5's commit, or commit separately once consumers exist:

```bash
git add apps/control/routing.py apps/control/admin.py config/asgi.py
git commit -m "feat(control): routing + ASGI wiring + admin"
```

---

## Task 5: `AgentControlConsumer` (agent-facing relay + registry + sweep)

**Files:**
- Create: `apps/control/consumers.py` (add `AgentControlConsumer`)
- Test: `tests/test_control_consumer_relay.py`

**Interfaces:**
- Consumes: `registry` (Task 2), `lock` (Task 3), `constants` (Task 3), `DeviceKey` verify (mirror `apps/tunnel/consumers.py:398-439`).
- Produces `AgentControlConsumer` at `ws/agent/control/<station_id>/`:
  - **connect:** parse query, fetch station, Ed25519 verify (else `close(4401)`); join `control_<id>_agent` + `control_<id>`; `accept()`; start `_sweep_loop`.
  - **receive** (agent→server): `inventory` → `registry.apply_inventory` + broadcast `control.inventory`; `state` → `registry.apply_state` (settings persist) + broadcast `control.state`; `result` → broadcast `control.result`; `event` → broadcast `control.event`.
  - **channel handler `control_to_agent`** (server→agent): `await self.send(text_data=json.dumps(event["frame"]))` — the verbatim §7 command/subscribe/unsubscribe/ptt_keepalive frame.
  - **disconnect:** cancel sweep; `registry.mark_station_offline`; free the lock (`lock.release`-equivalent full clear via a new `lock.force_free(station)`) + broadcast `control.lock` FREE + broadcast `control.agent_offline`; discard groups.
- Broadcast helper `_broadcast(msg_type, payload)` → `group_send(control_<id>, {"type": msg_type, **payload})` where `msg_type` is a channel handler name on `ControlConsumer` (e.g. `control.state`). Channels maps the dotted type to the method `control_state`.

Add to `lock.py`: `force_free(station, scope="station") -> bool` (atomic clear regardless of holder; returns True iff it was held) — used on agent disconnect. Include a unit test for it in `tests/test_control_lock.py`.

- [ ] **Step 1: Write the failing test (agent inventory → registry + broadcast)**

```python
# tests/test_control_consumer_relay.py
"""Channels tests for the control consumers. No pytest-asyncio — async
scenarios run via asyncio.run() inside @pytest.mark.django_db(transaction=True)."""

import asyncio

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.stations.models import Station
from config.asgi import application

V = 1


def _agent_comm(station_id):
    # Ed25519 verification is bypassed in tests by monkeypatching _verify_agent
    # (see conftest fixture control_agent_auth). Path still must match routing.
    return WebsocketCommunicator(application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0")


@pytest.mark.django_db(transaction=True)
def test_agent_inventory_updates_registry_and_broadcasts(control_agent_auth):
    from apps.control.models import StationModule

    station = Station.objects.create(name="ac1", status="online")

    async def scenario():
        layer = get_channel_layer()
        viewer_spy = "viewer-spy-1"
        await layer.group_add(f"control_{station.id}", viewer_spy)

        agent = _agent_comm(station.id)
        connected, _ = await agent.connect()
        assert connected is True

        await agent.send_json_to(
            {
                "v": V,
                "type": "inventory",
                "slots": [
                    {
                        "slot": "slot0",
                        "modules": [
                            {
                                "module": "fm0",
                                "identity": {"type": "fm", "model": "SA818", "version": "1"},
                                "capabilities": [
                                    {"name": "frequency", "kind": "setting", "type": "float"}
                                ],
                                "state": {"frequency": 145.5},
                            }
                        ],
                    }
                ],
            }
        )

        # Viewer group sees an inventory broadcast.
        evt = await layer.receive(viewer_spy)
        assert evt["type"] == "control.inventory"

        await agent.disconnect()

    asyncio.run(scenario())

    m = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert m.online is True
    assert m.last_state == {"frequency": 145.5}


@pytest.mark.django_db(transaction=True)
def test_agent_disconnect_marks_offline_and_frees_lock(control_agent_auth):
    from apps.control import lock
    from apps.control.models import StationModule

    station = Station.objects.create(name="ac2", status="online")
    holder = User.objects.create(username="h2", membership_level=User.MembershipLevel.MEMBER)
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)
    lock.acquire(station, holder)

    async def scenario():
        agent = _agent_comm(station.id)
        connected, _ = await agent.connect()
        assert connected is True
        await agent.disconnect()

    asyncio.run(scenario())

    assert StationModule.objects.filter(station=station, online=True).count() == 0
    lk = lock.get_or_create_lock(station)
    assert lk.holder_id is None  # freed on agent disconnect
```

Add the shared auth-bypass fixture to `tests/conftest.py` (create if missing):

```python
# tests/conftest.py  (append)
import pytest


@pytest.fixture
def control_agent_auth(monkeypatch):
    """Bypass Ed25519 verification for AgentControlConsumer in tests."""
    from apps.control import consumers

    async def _ok(self, station, params):
        return True

    monkeypatch.setattr(consumers.AgentControlConsumer, "_verify_agent", _ok)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_control_consumer_relay.py -v`
Expected: FAIL (`AttributeError`/`ImportError` — `apps.control.consumers` / `AgentControlConsumer` do not exist).

- [ ] **Step 3: Implement `AgentControlConsumer` in `consumers.py`**

```python
# apps/control/consumers.py
import asyncio
import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from . import constants, lock, registry

logger = logging.getLogger(__name__)


class AgentControlConsumer(AsyncWebsocketConsumer):
    """Agent-facing control WebSocket. Path: ws/agent/control/<station_id>/.

    Exactly one per station (one persistent agent Control-WS). Relays §7
    frames verbatim, updates the registry, and owns the lock sweep timer.
    """

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"control_{self.station_id}"
        self.agent_group_name = f"control_{self.station_id}_agent"
        self.sweep_task = None

        from urllib.parse import parse_qs

        query_string = self.scope.get("query_string", b"").decode()
        params = {k: v[0] for k, v in parse_qs(query_string).items() if v}

        station = await self._get_station()
        if station is None:
            await self.close(code=4404)
            return
        if not await self._verify_agent(station, params):
            await self.close(code=4401)
            return

        await self.channel_layer.group_add(self.agent_group_name, self.channel_name)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        self.sweep_task = asyncio.create_task(self._sweep_loop())

    async def disconnect(self, close_code):
        if getattr(self, "sweep_task", None):
            self.sweep_task.cancel()
            try:
                await self.sweep_task
            except asyncio.CancelledError:
                pass
            self.sweep_task = None

        station = await self._get_station()
        if station is not None:
            await self._mark_offline(station)
            freed = await self._force_free(station)
            await self._broadcast("control.agent_offline", {})
            if freed:
                status = await self._lock_status(station)
                await self._broadcast("control.lock", {"lock": status})

        await self.channel_layer.group_discard(self.agent_group_name, self.channel_name)
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return
        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        if mtype == "inventory":
            station = await self._get_station()
            if station is not None:
                await self._apply_inventory(station, msg.get("slots", []))
            await self._broadcast("control.inventory", {"msg": msg})
        elif mtype == "state":
            station = await self._get_station()
            if station is not None:
                await self._apply_state(
                    station, msg.get("slot"), msg.get("module"), msg.get("values", {})
                )
            await self._broadcast("control.state", {"msg": msg})
        elif mtype == "result":
            await self._broadcast("control.result", {"msg": msg})
        elif mtype == "event":
            await self._broadcast("control.event", {"msg": msg})
        # Unknown types are ignored (forward-compat).

    # -- server -> agent (channel handler) ------------------------------------

    async def control_to_agent(self, event):
        """A ControlConsumer relayed a §7 downstream frame -> send to agent."""
        await self.send(text_data=json.dumps(event["frame"]))

    # -- broadcasts we must ignore when echoed back to our own group ----------

    async def control_inventory(self, event):
        pass

    async def control_state(self, event):
        pass

    async def control_result(self, event):
        pass

    async def control_event(self, event):
        pass

    async def control_lock(self, event):
        pass

    async def control_agent_offline(self, event):
        pass

    # -- sweep loop -----------------------------------------------------------

    async def _sweep_loop(self):
        try:
            while True:
                await asyncio.sleep(constants.LOCK_SWEEP_INTERVAL_SECONDS)
                station = await self._get_station()
                if station is None:
                    continue
                freed = await self._sweep(station)
                if freed:
                    status = await self._lock_status(station)
                    await self._broadcast("control.lock", {"lock": status})
        except asyncio.CancelledError:
            raise

    async def _broadcast(self, msg_type, payload):
        await self.channel_layer.group_send(self.group_name, {"type": msg_type, **payload})

    # -- DB helpers -----------------------------------------------------------

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _apply_inventory(self, station, slots):
        registry.apply_inventory(station, slots)

    @database_sync_to_async
    def _apply_state(self, station, slot, module_id, values):
        registry.apply_state(station, slot, module_id, values)

    @database_sync_to_async
    def _mark_offline(self, station):
        registry.mark_station_offline(station)

    @database_sync_to_async
    def _force_free(self, station):
        return lock.force_free(station)

    @database_sync_to_async
    def _sweep(self, station):
        from django.utils import timezone

        return lock.sweep_lock(station, now=timezone.now(), idle_seconds=constants.T_IDLE_SECONDS)

    @database_sync_to_async
    def _lock_status(self, station):
        return lock.lock_status(lock.get_or_create_lock(station))

    @database_sync_to_async
    def _verify_agent(self, station, params):
        import hashlib
        import time

        from apps.api.models import DeviceKey

        signature = params.get("signature", "")
        timestamp = params.get("timestamp", "")
        if not signature or not timestamp:
            return False
        try:
            device_key = DeviceKey.objects.get(station=station, is_active=True)
        except DeviceKey.DoesNotExist:
            return False
        try:
            ts = float(timestamp)
        except (ValueError, TypeError):
            return False
        if time.time() - ts > 60 or ts > time.time() + 5:
            return False
        body_hash = hashlib.sha256(b"").hexdigest()
        signed_data = f"{timestamp}:{body_hash}".encode()
        if DeviceKey.verify_signature(device_key.current_public_key, signature, signed_data):
            return True
        if device_key.next_public_key and DeviceKey.verify_signature(
            device_key.next_public_key, signature, signed_data
        ):
            return True
        return False
```

Add `force_free` to `apps/control/lock.py`:

```python
@transaction.atomic
def force_free(station, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id is None:
        return False
    _clear(lock)
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_control_consumer_relay.py -v`
Expected: PASS (both agent tests).

- [ ] **Step 5: Commit (folds in Task 4 wiring)**

```bash
git add apps/control/consumers.py apps/control/routing.py apps/control/admin.py apps/control/lock.py config/asgi.py tests/conftest.py tests/test_control_consumer_relay.py
git commit -m "feat(control): AgentControlConsumer (relay + registry + lock sweep) + routing/asgi"
```

---

## Task 6: `ControlConsumer` (browser-facing: access, relay, lock, timeout)

**Files:**
- Modify: `apps/control/consumers.py` (add `ControlConsumer`)
- Test: `tests/test_control_consumer_lock.py`, `tests/test_control_consumer_relay.py` (extend with the browser→agent→viewer relay)

**Interfaces:**
- Consumes: `lock`, `registry`, `constants`, `StationModule`, `User.can_use_station` / `is_station_admin` / `can_administer_station` / `is_admin`, `StationAuditLog.log`.
- Produces `ControlConsumer` at `ws/control/<station_id>/`:
  - **connect:** `accept()`; reject-with-reason if anonymous (`4401`), not `can_use_station` (`4403`), station missing (`4404`), or viewers ≥ `MAX_VIEWERS_PER_STATION` (`4429`). On success: join `control_<id>`; send initial `inventory` snapshot (from registry) + current `lock` status; if this user is the lock holder reconnecting, `lock.holder_reconnected`.
  - **receive** (browser→server), dispatched by `type`:
    - `command` → require `can_use_station` **and** lock-holder (`lock.touch` returns holder-ness) → relay verbatim frame to `control_<id>_agent` via `control.to_agent`; start command-timeout timer keyed by `request_id`; audit `CONTROL_COMMAND`. Non-holder → `{type:"error", error:{code:"not_locked"}}`.
    - `ptt_keepalive` → require holder → `lock.touch` + relay verbatim; audit `CONTROL_PTT` (throttled/first only — see note).
    - `subscribe` / `unsubscribe` → require `can_use_station` only → relay verbatim (no lock needed).
    - `lock_acquire` → `lock.acquire`; broadcast `control.lock`; audit `CONTROL_LOCK_ACQUIRED` on success.
    - `lock_release` → `lock.release`; broadcast; audit `CONTROL_LOCK_RELEASED`.
    - `lock_request` → `lock.request_control`; if a holder exists, broadcast `control.control_requested` with requester `{id, username}` (each ControlConsumer forwards it to its browser only if it is the holder).
    - `lock_transfer` (`to_user_id`) → require current holder → `lock.transfer`; broadcast `control.lock`; audit `CONTROL_LOCK_TRANSFERRED`.
    - `lock_preempt` → require `is_station_admin`/`can_administer_station`/`is_admin` → `lock.preempt`; broadcast; audit `CONTROL_LOCK_PREEMPTED`. Non-admin → `{type:"error", error:{code:"forbidden"}}`.
  - **channel handlers** (broadcast→browser): `control_state`/`control_inventory`/`control_result`/`control_event`/`control_lock`/`control_agent_offline`/`control_control_requested` → forward the relevant JSON to this browser. `control_result` also cancels the matching pending command-timeout timer. `control_lock` adds a per-viewer `you_hold` bool. `control_control_requested` is forwarded only if this consumer's user is the current holder.
  - **disconnect:** cancel all pending timers; `lock.holder_disconnected(station, user, grace)` (starts reconnect-grace only if this user is holder and it was their last session — approximate by always calling; grace is cleared on any of the user's reconnects); discard group.

- **Audit note:** `CONTROL_PTT` keepalives arrive ~1/sec — do NOT audit every one (log-spam). Audit only PTT **on** (first keepalive after acquire / after idle) — implement by auditing PTT only when the frame is a `command` setting `ptt=true`, not on each keepalive. For D4 minimal: audit `command` frames whose `capability == "ptt"`; skip `ptt_keepalive` from audit entirely. Document this.

- **Add `StationAuditLog.EventType` members** (in `apps/stations/models.py`) + migration:
  `CONTROL_LOCK_ACQUIRED`, `CONTROL_LOCK_RELEASED`, `CONTROL_LOCK_TRANSFERRED`, `CONTROL_LOCK_PREEMPTED`, `CONTROL_COMMAND`, `CONTROL_PTT`.

- [ ] **Step 1: Add audit EventType members + migration**

In `apps/stations/models.py`, inside `StationAuditLog.EventType`, append:

```python
        CONTROL_LOCK_ACQUIRED = "control_lock_acquired", _("Control Lock Acquired")
        CONTROL_LOCK_RELEASED = "control_lock_released", _("Control Lock Released")
        CONTROL_LOCK_TRANSFERRED = "control_lock_transferred", _("Control Lock Transferred")
        CONTROL_LOCK_PREEMPTED = "control_lock_preempted", _("Control Lock Preempted")
        CONTROL_COMMAND = "control_command", _("Control Command")
        CONTROL_PTT = "control_ptt", _("Control PTT")
```

Run: `python manage.py makemigrations stations` → new migration adding the choices (a no-op DB change for TextField, but keep the migration for state consistency).

- [ ] **Step 2: Write failing access + lock Channels tests**

```python
# tests/test_control_consumer_lock.py
import asyncio

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.stations.models import Station
from config.asgi import application


def _browser_comm(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _drain_until(comm, msg_type, tries=6):
    """Read frames until one of msg_type arrives (skips initial snapshot/lock)."""
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"never saw {msg_type}")


@pytest.mark.django_db(transaction=True)
def test_applicant_rejected():
    station = Station.objects.create(name="cc1", status="online")
    user = User.objects.create(username="app1", membership_level=User.MembershipLevel.APPLICANT)

    async def scenario():
        comm = _browser_comm(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True
        msg = await comm.receive_json_from()
        assert msg["type"] == "error"
        assert msg["code"] == 4403
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_acquire_broadcasts_lock_and_non_holder_command_rejected():
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cc2", status="online")
    holder = User.objects.create(username="h", membership_level=User.MembershipLevel.MEMBER)
    other = User.objects.create(username="o", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-cc2"
        await layer.group_add(f"control_{station.id}_agent", agent_spy)

        hc = _browser_comm(holder, station.id)
        oc = _browser_comm(other, station.id)
        assert (await hc.connect())[0] is True
        assert (await oc.connect())[0] is True

        await hc.send_json_to({"type": "lock_acquire"})
        lock_evt = await _drain_until(hc, "lock")
        assert lock_evt["state"] == "held"
        assert lock_evt["you_hold"] is True

        # Non-holder command is rejected, never reaches the agent group.
        await oc.send_json_to(
            {"type": "command", "request_id": "r1", "slot": "slot0",
             "module": "fm0", "capability": "frequency", "op": "set", "value": 145.5}
        )
        err = await _drain_until(oc, "error")
        assert err["error"]["code"] == "not_locked"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(layer.receive(agent_spy), timeout=0.3)

        await hc.disconnect()
        await oc.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_holder_command_relayed_to_agent():
    station = Station.objects.create(name="cc3", status="online")
    holder = User.objects.create(username="h3", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-cc3"
        await layer.group_add(f"control_{station.id}_agent", agent_spy)

        hc = _browser_comm(holder, station.id)
        assert (await hc.connect())[0] is True
        await hc.send_json_to({"type": "lock_acquire"})
        await _drain_until(hc, "lock")

        frame = {"type": "command", "request_id": "r9", "slot": "slot0",
                 "module": "fm0", "capability": "frequency", "op": "set", "value": 145.5}
        await hc.send_json_to(frame)

        relayed = await layer.receive(agent_spy)
        assert relayed["type"] == "control.to_agent"
        assert relayed["frame"]["request_id"] == "r9"
        assert relayed["frame"]["value"] == 145.5

        await hc.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_admin_preempt_takes_lock():
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cc4", status="online")
    holder = User.objects.create(username="h4", membership_level=User.MembershipLevel.MEMBER)
    admin = User.objects.create(username="admin4", membership_level=User.MembershipLevel.ADMIN)
    lockmod.acquire(station, holder)

    async def scenario():
        ac = _browser_comm(admin, station.id)
        assert (await ac.connect())[0] is True
        await ac.send_json_to({"type": "lock_preempt"})
        evt = await _drain_until(ac, "lock")
        assert evt["state"] == "held"
        assert evt["holder_id"] == admin.id
        await ac.disconnect()

    asyncio.run(scenario())
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_control_consumer_lock.py -v`
Expected: FAIL (`ControlConsumer` not defined / connection not accepted as expected).

- [ ] **Step 4: Implement `ControlConsumer`**

Append to `apps/control/consumers.py`:

```python
class ControlConsumer(AsyncWebsocketConsumer):
    """Browser-facing control WebSocket. Path: ws/control/<station_id>/.

    Access-controlled (can_use_station). Relays holder commands/PTT and lock
    actions to the agent; pushes state/inventory/result/event + lock status to
    all viewers.
    """

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"control_{self.station_id}"
        self.agent_group_name = f"control_{self.station_id}_agent"
        self.pending = {}  # request_id -> asyncio.Task (command timeout)
        self.user = self.scope.get("user")

        await self.accept()

        if not self.user or self.user.is_anonymous:
            await self._reject(4401, "Not signed in — please sign in again")
            return
        station = await self._get_station()
        if station is None:
            await self._reject(4404, "Station not found")
            return
        if not await self._can_use(station):
            await self._reject(4403, "You are not permitted to control this station")
            return
        if await self._viewer_count() >= constants.MAX_VIEWERS_PER_STATION:
            await self._reject(4429, "Too many active viewers for this station")
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        # Reconnect within grace keeps a held lock.
        await self._holder_reconnected(station)
        # Initial snapshot + lock status to just this browser.
        await self.send(text_data=json.dumps({"type": "inventory", "modules": await self._snapshot(station)}))
        await self._send_lock_status(station)

    async def _reject(self, code, reason):
        try:
            await self.send(text_data=json.dumps({"type": "error", "reason": reason, "code": code}))
        finally:
            await self.close(code=code)

    async def disconnect(self, close_code):
        for task in list(self.pending.values()):
            task.cancel()
        self.pending.clear()
        station = await self._get_station()
        if station is not None and self.user and not self.user.is_anonymous:
            await self._holder_disconnected(station)
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return
        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        station = await self._get_station()
        if station is None:
            return

        if mtype == "command":
            await self._handle_command(station, msg)
        elif mtype == "ptt_keepalive":
            if await self._touch_if_holder(station):
                await self._relay(msg)
            else:
                await self._error(msg.get("request_id"), "not_locked", "You do not hold the lock")
        elif mtype in ("subscribe", "unsubscribe"):
            await self._relay(msg)  # access already checked at connect; any viewer may (un)subscribe
        elif mtype == "lock_acquire":
            await self._lock_acquire(station)
        elif mtype == "lock_release":
            await self._lock_release(station)
        elif mtype == "lock_request":
            await self._lock_request(station)
        elif mtype == "lock_transfer":
            await self._lock_transfer(station, msg.get("to_user_id"))
        elif mtype == "lock_preempt":
            await self._lock_preempt(station)

    # -- command + timeout ----------------------------------------------------

    async def _handle_command(self, station, msg):
        if not await self._touch_if_holder(station):
            await self._error(msg.get("request_id"), "not_locked", "You do not hold the lock")
            return
        await self._relay(msg)
        request_id = msg.get("request_id")
        if request_id is not None:
            self.pending[request_id] = asyncio.create_task(self._command_timeout(request_id))
        await self._audit(station, "control_command",
                          f"{self.user.username} {msg.get('op')} {msg.get('capability')}")

    async def _command_timeout(self, request_id):
        try:
            await asyncio.sleep(constants.COMMAND_TIMEOUT_SECONDS)
            await self.send(text_data=json.dumps(
                {"type": "error", "request_id": request_id,
                 "error": {"code": "timeout", "msg": "No result from agent"}}))
            self.pending.pop(request_id, None)
        except asyncio.CancelledError:
            raise

    async def _relay(self, frame):
        await self.channel_layer.group_send(
            self.agent_group_name, {"type": "control.to_agent", "frame": frame})

    async def _error(self, request_id, code, msg):
        await self.send(text_data=json.dumps(
            {"type": "error", "request_id": request_id, "error": {"code": code, "msg": msg}}))

    # -- lock actions ---------------------------------------------------------

    async def _lock_acquire(self, station):
        if await self._acquire(station):
            await self._audit(station, "control_lock_acquired", f"{self.user.username} acquired control")
        await self._broadcast_lock(station)

    async def _lock_release(self, station):
        if await self._release(station):
            await self._audit(station, "control_lock_released", f"{self.user.username} released control")
        await self._broadcast_lock(station)

    async def _lock_request(self, station):
        holder = await self._request(station)
        if holder is not None:
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "control.control_requested",
                 "holder_id": holder.holder_id,
                 "requester": {"id": self.user.id, "username": self.user.username}})

    async def _lock_transfer(self, station, to_user_id):
        if to_user_id is not None and await self._transfer(station, to_user_id):
            await self._audit(station, "control_lock_transferred",
                              f"{self.user.username} -> user {to_user_id}")
        await self._broadcast_lock(station)

    async def _lock_preempt(self, station):
        if not await self._can_administer(station):
            await self._error(None, "forbidden", "Admin rights required to preempt")
            return
        await self._preempt(station)
        await self._audit(station, "control_lock_preempted", f"{self.user.username} preempted control")
        await self._broadcast_lock(station)

    async def _broadcast_lock(self, station):
        status = await self._lock_status(station)
        await self.channel_layer.group_send(self.group_name, {"type": "control.lock", "lock": status})

    async def _send_lock_status(self, station):
        status = await self._lock_status(station)
        await self._push_lock(status)

    async def _push_lock(self, status):
        payload = dict(status)
        payload["type"] = "lock"
        payload["you_hold"] = bool(self.user and status.get("holder_id") == self.user.id)
        await self.send(text_data=json.dumps(payload))

    # -- channel handlers (broadcast -> this browser) -------------------------

    async def control_state(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_inventory(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_result(self, event):
        msg = event["msg"]
        rid = msg.get("request_id")
        task = self.pending.pop(rid, None)
        if task is not None:
            task.cancel()
        await self.send(text_data=json.dumps(msg))

    async def control_event(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_lock(self, event):
        await self._push_lock(event["lock"])

    async def control_agent_offline(self, event):
        await self.send(text_data=json.dumps({"type": "agent_offline"}))

    async def control_control_requested(self, event):
        # Only the current holder should be prompted.
        if self.user and event.get("holder_id") == self.user.id:
            await self.send(text_data=json.dumps(
                {"type": "control_requested", "requester": event["requester"]}))

    async def control_to_agent(self, event):
        pass  # not for browsers

    # -- DB helpers -----------------------------------------------------------

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _can_use(self, station):
        return bool(self.user) and not self.user.is_anonymous and self.user.can_use_station(station)

    @database_sync_to_async
    def _can_administer(self, station):
        return self.user.is_admin or self.user.is_station_admin(station) or self.user.can_administer_station(station)

    @database_sync_to_async
    def _viewer_count(self):
        # Channel-group membership isn't directly countable; approximate with a
        # cheap upper bound of 0 here and rely on group size being small. For a
        # hard cap, track ControlSession rows (future). For D4 we enforce via a
        # per-connection in-memory set is impossible cross-process, so return 0
        # (cap effectively disabled unless CONTROL_MAX_VIEWERS_PER_STATION logic
        # is backed by a session table). See Task 6 note.
        return 0

    @database_sync_to_async
    def _snapshot(self, station):
        from .models import StationModule

        out = []
        for m in StationModule.objects.filter(station=station):
            out.append({
                "slot": m.slot, "module": m.module_id,
                "identity": {"type": m.type, "model": m.model, "version": m.version},
                "capabilities": m.capability_descriptor,
                "state": m.last_state, "online": m.online,
            })
        return out

    @database_sync_to_async
    def _acquire(self, station):
        return lock.acquire(station, self.user)

    @database_sync_to_async
    def _release(self, station):
        return lock.release(station, self.user)

    @database_sync_to_async
    def _request(self, station):
        return lock.request_control(station, self.user)

    @database_sync_to_async
    def _transfer(self, station, to_user_id):
        return lock.transfer(station, self.user, to_user_id)

    @database_sync_to_async
    def _preempt(self, station):
        return lock.preempt(station, self.user)

    @database_sync_to_async
    def _touch_if_holder(self, station):
        return lock.touch(station, self.user)

    @database_sync_to_async
    def _holder_disconnected(self, station):
        lock.holder_disconnected(station, self.user, constants.RECONNECT_GRACE_SECONDS)

    @database_sync_to_async
    def _holder_reconnected(self, station):
        lock.holder_reconnected(station, self.user)

    @database_sync_to_async
    def _lock_status(self, station):
        return lock.lock_status(lock.get_or_create_lock(station))

    @database_sync_to_async
    def _audit(self, station, event_type, message):
        from apps.stations.models import StationAuditLog

        StationAuditLog.log(station=station, event_type=event_type, message=message, user=self.user)
```

**Viewer-cap note:** the cross-process viewer cap needs a `ControlSession` table to count reliably (like `TerminalSession`). For D4 the cap is defined in config but enforcement returns 0 (effectively off) to avoid an unreliable per-process count. If a hard cap is required, add a `ControlSession` model in a follow-up task mirroring `TerminalSession` reap/count. This is documented as a known limitation in the PR. **Decision point:** if the reviewer wants the cap enforced now, implement `ControlSession` (see Task 6b optional).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_control_consumer_lock.py -v`
Expected: PASS (access reject, acquire+non-holder-reject, holder relay, admin preempt).

- [ ] **Step 6: Commit**

```bash
git add apps/control/consumers.py apps/stations/models.py apps/stations/migrations/ tests/test_control_consumer_lock.py
git commit -m "feat(control): ControlConsumer (access + lock + relay + command timeout + audit)"
```

---

## Task 7: End-to-end relay + edge tests (mock agent ↔ browser)

**Files:**
- Test: `tests/test_control_consumer_relay.py` (extend)

**Interfaces:**
- Consumes both consumers. Verifies the full loop from the spec §8 data flow.

- [ ] **Step 1: Write E2E relay + timeout + control_requested tests**

```python
# tests/test_control_consumer_relay.py  (append)

def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _until(comm, mtype, tries=8):
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"never saw {mtype}")


@pytest.mark.django_db(transaction=True)
def test_full_relay_command_result_state_to_all_viewers(control_agent_auth):
    from apps.control import lock as lockmod

    station = Station.objects.create(name="e2e1", status="online")
    holder = User.objects.create(username="he", membership_level=User.MembershipLevel.MEMBER)
    viewer = User.objects.create(username="ve", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        # seed registry so state persist has a descriptor
        await agent.send_json_to({
            "v": V, "type": "inventory",
            "slots": [{"slot": "slot0", "modules": [{
                "module": "fm0", "identity": {"type": "fm"},
                "capabilities": [{"name": "frequency", "kind": "setting", "type": "float"}],
                "state": {"frequency": 145.0}}]}]})

        hc = _browser(holder, station.id)
        vc = _browser(viewer, station.id)
        assert (await hc.connect())[0] is True
        assert (await vc.connect())[0] is True

        await hc.send_json_to({"type": "lock_acquire"})
        await _until(hc, "lock")

        await hc.send_json_to({"type": "command", "request_id": "rq", "slot": "slot0",
                               "module": "fm0", "capability": "frequency", "op": "set", "value": 146.5})
        # Agent receives the relayed command frame.
        got = await agent.receive_json_from()
        assert got["type"] == "command" and got["request_id"] == "rq"

        # Agent responds with result + state.
        await agent.send_json_to({"v": V, "type": "result", "request_id": "rq", "ok": True, "value": 146.5})
        await agent.send_json_to({"v": V, "type": "state", "slot": "slot0", "module": "fm0",
                                  "values": {"frequency": 146.5}, "ts": 1.0})

        # Both holder and viewer see the state broadcast.
        hstate = await _until(hc, "state")
        vstate = await _until(vc, "state")
        assert hstate["values"]["frequency"] == 146.5
        assert vstate["values"]["frequency"] == 146.5

        await hc.disconnect()
        await vc.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())

    from apps.control.models import StationModule
    m = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert m.last_state == {"frequency": 146.5}  # settings persisted


@pytest.mark.django_db(transaction=True)
def test_command_timeout_pushes_error(control_agent_auth, settings):
    settings.CONTROL_COMMAND_TIMEOUT_SECONDS = 0  # fire immediately
    # Re-read constants after override.
    import importlib
    from apps.control import constants
    importlib.reload(constants)

    station = Station.objects.create(name="e2e2", status="online")
    holder = User.objects.create(username="ht", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        hc = _browser(holder, station.id)
        assert (await hc.connect())[0] is True
        await hc.send_json_to({"type": "lock_acquire"})
        await _until(hc, "lock")
        await hc.send_json_to({"type": "command", "request_id": "to1", "slot": "slot0",
                               "module": "fm0", "capability": "frequency", "op": "set", "value": 1.0})
        err = await _until(hc, "error")
        assert err["error"]["code"] == "timeout"
        assert err["request_id"] == "to1"
        await hc.disconnect()

    asyncio.run(scenario())
    importlib.reload(constants)  # restore default for other tests
```

**Note on `importlib.reload(constants)`:** because `constants.py` reads settings at import time, tests overriding a tunable must reload it. Prefer this over re-architecting; document in the test. Alternatively, change `_command_timeout` to read `getattr(settings, "CONTROL_COMMAND_TIMEOUT_SECONDS", 10)` at call time so the `settings` fixture works without reload — **implement this call-time read** to keep the test simple:

In `ControlConsumer._command_timeout`, replace `constants.COMMAND_TIMEOUT_SECONDS` with:
```python
from django.conf import settings as dj_settings
timeout = getattr(dj_settings, "CONTROL_COMMAND_TIMEOUT_SECONDS", 10)
await asyncio.sleep(timeout)
```
Then the `settings` fixture alone suffices; drop the `importlib.reload` lines.

- [ ] **Step 2: Run tests to verify they fail, then pass**

Run: `pytest tests/test_control_consumer_relay.py -v`
Expected: after applying the call-time-read tweak, PASS (all relay + timeout tests).

- [ ] **Step 3: Full control test suite green**

Run: `pytest tests/test_control_registry.py tests/test_control_lock.py tests/test_control_consumer_lock.py tests/test_control_consumer_relay.py -v`
Expected: PASS (all).

- [ ] **Step 4: Commit**

```bash
git add apps/control/consumers.py tests/test_control_consumer_relay.py
git commit -m "test(control): E2E relay (browser->agent->result/state->all viewers) + command timeout"
```

---

## Task 8: Full verification + lint + migration check

**Files:** none (verification only).

- [ ] **Step 1: Run the whole test suite**

Run: `pytest -q`
Expected: no regressions; all control tests pass.

- [ ] **Step 2: Check for missing migrations**

Run: `python manage.py makemigrations --check --dry-run`
Expected: `No changes detected` (all model changes are already migrated).

- [ ] **Step 3: Lint / format (match repo tooling)**

Run: `ruff check apps/control tests/test_control_*.py && ruff format --check apps/control tests/test_control_*.py`
Expected: clean (fix any findings, re-run).

- [ ] **Step 4: ASGI import smoke test**

Run: `DJANGO_SETTINGS_MODULE=config.settings.test python -c "import django; django.setup(); import config.asgi; print('asgi ok')"`
Expected: `asgi ok`.

- [ ] **Step 5: Commit any fixups**

```bash
git add -A
git commit -m "chore(control): lint + verification fixups"
```

---

## Task 9: PR

- [ ] **Step 1: Push branch**

```bash
git push -u origin feature/d4-server-control
```

- [ ] **Step 2: Open PR with `Closes #90`**

PR body must:
- Summarize the two consumers, registry, lock, access mapping.
- **Explicitly call out the gating decision** (command/PTT lock-gated; subscribe/unsubscribe access-gated) for reviewer sign-off.
- **Call out the viewer-cap limitation** (config present, hard enforcement deferred to a `ControlSession` follow-up) — or, if implemented, link Task 6b.
- Link the design spec `docs/superpowers/specs/2026-07-08-server-control-registry-and-lock-design.md`.
- End with `Closes #90`.

- [ ] **Step 3: Run the copilot-loop**

Per CLAUDE.md, station-manager PRs need several review rounds. Use `~/.claude/skills/copilot-loop/` (4 min initial wait, 1 min poll, 10 min total; code-quality reviews on Opus).

---

## Optional Task 6b: `ControlSession` for a hard viewer cap (only if reviewer requires)

Mirror `apps/tunnel/models.TerminalSession`: `ControlSession(station, user, channel_name, status, started_at, last_seen, close_reason)`. In `ControlConsumer.connect`, reap stale + count active < `MAX_VIEWERS_PER_STATION` (reject 4429 otherwise), create a row, keepalive-touch loop, close on disconnect. Add a Channels test `test_control_viewer_cap`. Only build this if the PR review asks for enforced caps — otherwise YAGNI.

---

## Self-Review (completed against the spec)

- **§3 two consumers + channel-group relay** → Tasks 4/5/6. `control_<id>` + `control_<id>_agent`. ✔
- **§4 StationModule registry (unique slot/module, descriptor+last_state persistent, telemetry ephemeral, upsert, soft-offline)** → Tasks 1/2. ✔
- **§5 TX-lock (FREE/HELD, user-owned, cooperative release/targeted transfer, auto-free disconnect+grace + T_idle, admin preemption, holder-only command/PTT)** → Tasks 3/6. ✔ Two timers: `T_idle` (sweep) + reconnect-grace (`pending_release_at`); `T_ptt` is agent-local (D3), out of scope. ✔
- **§6 access mapping (can_use_station to see+control; admin to preempt)** → Task 6. ✔
- **§7 edge cases (agent-disconnect→lock free+offline; command timeout→error; audit)** → Tasks 5/6/7. ✔
- **§8 data flow** → Task 7 E2E. ✔
- **§10 config (T_idle, grace, max-viewer, command timeout)** → Task 3 constants + settings; viewer-cap enforcement flagged as deferred. ✔
- **§11 testing (relay/registry/lock/access/edge)** → Tasks 2/3/5/6/7. ✔
- **Verbatim relay** (no §7 transformation) → command/subscribe/unsubscribe/ptt/state/result/event forwarded as whole frames. ✔

**Known deferred (documented in PR):** hard viewer-cap enforcement (needs `ControlSession`); per-module lock / teacher-student / license bit (explicitly out of scope, model left extensible via `scope`).
