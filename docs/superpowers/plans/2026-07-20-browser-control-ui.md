# Browser-Control-UI / Generic Renderer (D5) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. UI work (CSS/JS/templates) MUST invoke `Skill("frontend-design")`.

**Goal:** A generic, module-agnostic browser control panel that renders from the `StationModule.capability_descriptor`, connects the D4 control WebSocket, lets the lock-holder command modules + push-to-talk (PTT), and shows live telemetry + lock status — browser → agent → radio, end-to-end (control + key + telemetry). No voice audio (that is D6–D9).

**Architecture:** A server-rendered Django page (`/stations/<id>/control/`) iterates each module's capability descriptor into Bootstrap/rack-console widgets (offline-render from `last_state` for free). An additive **Alpine.js island** (Alpine 3.14.1 is already loaded globally) holds reactive lock/connection/TX/value state and owns a **WebSocket client** to `ws/control/<id>/` (D4). The descriptor drives everything; there is **no `fm`/`frequency` hardcode** anywhere in the renderer or JS. All JS logic lives in a static file registered via `alpine:init` — the repo convention is **no inline `<script>` logic**.

**Tech Stack:** Django 6.0, Django Channels (D4 consumers, unchanged), Bootstrap 5, Alpine.js 3.14.1 (already in `base.html`), IBM Plex Mono/Sans + Bricolage Grotesque, vanilla WebSocket. Tests: pytest + Channels `WebsocketCommunicator` (protocol-level E2E, runs in CI), Django test client (render tests), optional pytest-playwright (browser PTT/lock flows, gated on browser availability).

## Global Constraints

- **Base branch:** `feature/d5-browser-control-ui` (worktree `.worktrees/d5-browser-control-ui`), based on `origin/feature/d4-server-control`. One branch, one PR, `Closes #99`.
- **No `fm`/`frequency` string in renderer or JS logic** — everything is descriptor-driven. Generizität is tested with a **second fictitious module** that appears with zero UI-code changes.
- **DE-locale float safety:** every numeric `<input type="number">` sets `lang="en"` + `inputmode="decimal"`; values are always sent/parsed as **dot-decimal** (CLAUDE.md rule). Never rely on the browser locale to parse the frequency.
- **CSP:** inline `<script>` blocks are forbidden by convention; any inline `<style>`/`<script>` that is unavoidable must carry `nonce="{{ csp_nonce }}"`. Prefer static files.
- **No-inline-JS convention:** all behavior lives in `static/js/control-panel.js` (loaded via `<script src>`), registering `Alpine.data('controlPanel', …)` on the `alpine:init` event. Alpine attribute expressions in templates are allowed (they are HTML, not `<script>`).
- **Widget↔JS contract:** every widget carries `data-slot`, `data-module`, `data-cap`, `data-kind`, `data-type`; interactive Alpine expressions pass those values as string literals (Django-rendered) to component methods. The JS never assumes any specific capability name **except** the platform-level `ptt` capability (`kind:"action"` + `type:"bool"` + `name:"ptt"`), which is a cross-module platform contract (D3 §8), not a module hardcode.
- **Command feedback is not blind-optimistic:** a `command` marks the widget *pending* → `result` ack/error → the authoritative `state` push confirms the actual value. Never assume success on send.
- **PTT is fail-safe:** any of release / `mouseleave` / blur / WS-drop / lock-loss → immediately send `ptt set false` + stop keepalive; the agent-side dead-man is the backstop, never the only guard.
- **Commit frequently** (per task). Run `ruff` + the relevant tests before each commit. `ruff check . && ruff format --check .` must pass.

## The D4 contract this panel consumes (reference — do not re-implement)

Browser WS: `ws/control/<station_id>/` (`apps/control/consumers.py::ControlConsumer`, Django-session auth, gated on `can_use_station`).

**Server → browser frames:**
- `{"v":1,"type":"inventory","slots":[{"slot","modules":[{"module","identity":{"type","model","version"},"capabilities":[<descriptor>],"state":{cap:val},"online":bool}]}]}` — sent as the first frame on connect (persisted snapshot; works offline), and again live on topology change.
- `{"type":"state","slot","module","values":{cap:val},"ts"}` — value push after a command / telemetry tick.
- `{"type":"result","request_id","ok"?,"value"?,"error"?:{code,msg}}` — command result.
- `{"type":"event","slot","module","event","detail"}` — async (e.g. `ptt_auto_unkey`, `module_added`, `module_removed`, `module_error`).
- `{"type":"lock","state":"free"|"held","holder_id","holder_username","since","you_hold":bool}` — sent on connect (free/held) and on every lock mutation. Identical shape every time → one handler.
- `{"type":"error","request_id"?,"reason"?,"code"?,"error"?:{code,msg}}` — includes command `timeout` and `not_locked`.
- `{"type":"agent_offline"}` — the station agent disconnected while this browser is connected.
- `{"type":"control_requested","requester":{"id","username"}}` — sent **only to the current holder** when another user asks for control.

**Browser → server frames:**
- `{"type":"command","request_id","slot","module","capability","op":"set"|"get"|"do","value"?}`
- `{"type":"ptt_keepalive","slot","module"}` (holder-only; feeds the dead-man)
- `{"type":"subscribe","slot","module","capabilities":[…],"interval_ms"}` / `{"type":"unsubscribe","slot","module","capabilities":[…]}` (any viewer — access-gated, not lock-gated)
- `{"type":"lock_acquire"}` / `{"type":"lock_release"}` / `{"type":"lock_request"}` / `{"type":"lock_transfer","to_user_id"}` / `{"type":"lock_preempt"}` (preempt is admin-only, server-enforced)

**Capability descriptor schema** (authoritative — from `station_agent/descriptor.py`):
```json
{"name":"frequency","kind":"setting","type":"float","ranges":[{"name":"vhf","min":134.0,"max":174.0}]}
{"name":"volume","kind":"setting","type":"int","ranges":[{"min":1,"max":8}]}
{"name":"power_level","kind":"setting","type":"enum","values":["low","high"]}
{"name":"ptt","kind":"action","type":"bool"}
{"name":"rssi","kind":"telemetry","type":"int","readonly":true,"min_interval_ms":250}
{"name":"band","kind":"telemetry","type":"string","readonly":true}
```
- `kind ∈ {setting, action, telemetry}`; `type ∈ {float, int, enum, bool, string}`.
- Optional cap fields: `ranges:[{name?,min,max}]` (numeric bounds; may be multiple named bands), `values:[…]` (enum), `readonly:bool`, `min_interval_ms:int` (telemetry rate floor), `unit:str` (optional display unit; render if present), `step:number` (optional UI step; if absent derive: `int`→1, `float`→see Task 3).
- Op gating (mirror, for enabling controls): `setting`→set/get, `action`→do/get, `telemetry`→get. A `readonly` setting renders as display-only.

---

## File Structure

**New files**
- `apps/control/views.py` — `StationControlView` (access-gated page).
- `apps/control/urls.py` — `app_name="control"`, route `<int:pk>/control/` → `station_control`.
- `apps/control/serializers.py` — `snapshot(station) -> list` (shared inventory builder; DRY with the consumer).
- `apps/control/templates/control/panel.html` — the page (extends `base.html`).
- `apps/control/templates/control/_module_card.html` — one rack-module card.
- `apps/control/templates/control/_lock_banner.html` — lock/connection banner.
- `apps/control/templates/control/widgets/_widget.html` — dispatch partial (kind/type/name → specific widget).
- `apps/control/templates/control/widgets/_number.html` — setting float/int (input + step buttons, bounded).
- `apps/control/templates/control/widgets/_enum.html` — setting enum (select).
- `apps/control/templates/control/widgets/_bool.html` — setting bool (toggle).
- `apps/control/templates/control/widgets/_text.html` — setting string (text input) / readonly display.
- `apps/control/templates/control/widgets/_action.html` — action non-ptt (button).
- `apps/control/templates/control/widgets/_ptt.html` — action ptt (push-and-hold bar).
- `apps/control/templates/control/widgets/_telemetry.html` — telemetry meter/readout.
- `static/js/control-panel.js` — Alpine component + WS client + PTT + lock logic.
- `static/css/control-panel.css` — panel styles (rack faces, readout, PTT bar, meters, lock banner, offline states).
- `tests/test_control_views.py` — access + render + generizität + offline + DE-locale.
- `tests/test_control_panel_ws.py` — Channels protocol-level E2E (command→state, subscribe, lock, ptt relay, agent_offline).
- `tests/e2e/test_control_panel_browser.py` — optional pytest-playwright PTT/lock/WS-drop flows (skips if browser unavailable).

**Modified files**
- `config/urls.py` — include `apps.control.urls` at `stations/`.
- `apps/control/consumers.py` — `ControlConsumer._snapshot` delegates to `serializers.snapshot` (DRY, no behavior change).
- `apps/stations/templates/stations/station_detail.html` — add a "Control panel" link (gated).
- `templates/base.html` — nothing (Alpine already present); the page pulls its CSS/JS via `extra_head`/`extra_scripts` blocks.

---

## Task 1: Route, view, access gate, and station_detail link

**Files:**
- Create: `apps/control/views.py`, `apps/control/urls.py`, `apps/control/serializers.py`
- Create: `apps/control/templates/control/panel.html` (minimal skeleton this task)
- Modify: `config/urls.py`, `apps/control/consumers.py`, `apps/stations/templates/stations/station_detail.html`
- Test: `tests/test_control_views.py`

**Interfaces:**
- Produces: `apps.control.serializers.snapshot(station) -> list[dict]` — same slots-grouped shape the consumer sends as `inventory` (`[{"slot","modules":[{"module","identity","capabilities","state","online"}]}]`).
- Produces: URL name `control:station_control` (kwarg `pk`), template context `station`, `modules` (`station.modules.all()` ordered), `initial_inventory` (= `snapshot(station)`), `can_admin` (bool), `ptt_default_key` (`" "`).
- Consumes: `apps.control.registry`, `StationModule`, `User.can_use_station`, `User.is_admin/is_station_admin/can_administer_station`.

- [ ] **Step 1: Write the failing render/access test**

```python
# tests/test_control_views.py
import pytest
from django.urls import reverse
from apps.accounts.models import User
from apps.stations.models import Station
from apps.control.models import StationModule

FM = [
    {"name": "frequency", "kind": "setting", "type": "float",
     "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}]},
    {"name": "ptt", "kind": "action", "type": "bool"},
    {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 250},
]

@pytest.fixture
def station(db):
    return Station.objects.create(name="s1", status="online")

@pytest.fixture
def operator(db, station):
    u = User.objects.create_user(username="op", password="x")
    # Grant can_use_station via the project's assignment mechanism.
    from apps.stations.models import StationAssignment
    StationAssignment.objects.create(user=u, station=station)
    return u

def test_anonymous_redirected(client, station):
    r = client.get(reverse("control:station_control", args=[station.pk]))
    assert r.status_code in (301, 302)

def test_permitted_user_gets_panel(client, station, operator):
    StationModule.objects.create(station=station, slot="slot0", module_id="fm",
                                 type="fm", capability_descriptor=FM,
                                 last_state={"frequency": 145.5}, online=True)
    client.force_login(operator)
    r = client.get(reverse("control:station_control", args=[station.pk]))
    assert r.status_code == 200
    assert b'data-cap="frequency"' in r.content
    assert b'id="control-panel"' in r.content

def test_forbidden_user_gets_403(client, station):
    other = User.objects.create_user(username="no", password="x")
    client.force_login(other)
    r = client.get(reverse("control:station_control", args=[station.pk]))
    assert r.status_code == 403
```

- [ ] **Step 2: Run test — expect failure** (`NoReverseMatch` / 404). `pytest tests/test_control_views.py -q`

- [ ] **Step 3: Implement `serializers.snapshot` + refactor consumer to use it**

```python
# apps/control/serializers.py
"""Shared, pure inventory serialization: the connect-time snapshot the browser
consumer sends AND the SSR page's initial state come from one builder (DRY)."""
from .models import StationModule


def snapshot(station):
    """Return the persisted inventory grouped by slot, in the same shape as the
    agent's live ``inventory`` frame. Includes a per-module ``online`` flag so
    offline modules still render from persisted descriptors + last_state."""
    slots, order = {}, []
    for m in StationModule.objects.filter(station=station):
        if m.slot not in slots:
            slots[m.slot] = []
            order.append(m.slot)
        slots[m.slot].append({
            "module": m.module_id,
            "identity": {"type": m.type, "model": m.model, "version": m.version},
            "capabilities": m.capability_descriptor,
            "state": m.last_state,
            "online": m.online,
        })
    return [{"slot": s, "modules": slots[s]} for s in order]
```
Then in `apps/control/consumers.py`, replace the body of `ControlConsumer._snapshot` with `from . import serializers; return serializers.snapshot(station)` (keep the `@database_sync_to_async` wrapper). Verify D4 tests still pass.

- [ ] **Step 4: Implement the view + urls + config include**

```python
# apps/control/views.py
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import DetailView
from apps.stations.models import Station
from . import serializers


class StationControlView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = Station
    template_name = "control/panel.html"
    context_object_name = "station"
    raise_exception = True  # 403 for authenticated-but-unauthorized, not a redirect loop

    def test_func(self):
        return self.request.user.can_use_station(self.get_object())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        station = self.object
        ctx["modules"] = list(station.modules.all())
        ctx["initial_inventory"] = serializers.snapshot(station)
        u = self.request.user
        ctx["can_admin"] = (
            u.is_admin or u.is_station_admin(station) or u.can_administer_station(station)
        )
        ctx["ptt_default_key"] = " "
        return ctx
```
```python
# apps/control/urls.py
from django.urls import path
from . import views

app_name = "control"
urlpatterns = [
    path("<int:pk>/control/", views.StationControlView.as_view(), name="station_control"),
]
```
`config/urls.py`: add `path("stations/", include("apps.control.urls"))` **after** the existing stations include (order is fine; distinct sub-paths). Confirm `reverse("control:station_control", args=[1]) == "/stations/1/control/"`.

- [ ] **Step 5: Minimal `panel.html` skeleton (expanded in later tasks)**

```django
{% extends "base.html" %}
{% load i18n static %}
{% block title %}{% trans "Control" %} · {{ station.name }}{% endblock %}
{% block extra_head %}<link rel="stylesheet" href="{% static 'css/control-panel.css' %}" nonce="{{ csp_nonce }}">{% endblock %}
{% block content %}
  {{ initial_inventory|json_script:"control-initial" }}
  <div id="control-panel"
       x-data="controlPanel()"
       data-station-id="{{ station.id }}"
       data-user-id="{{ request.user.id }}"
       data-can-admin="{{ can_admin|yesno:'1,0' }}"
       data-ptt-default-key="{{ ptt_default_key }}">
    {% include "control/_lock_banner.html" %}
    <div class="cp-modules">
      {% for m in modules %}{% include "control/_module_card.html" %}{% empty %}
        <p class="cp-empty">{% trans "No modules discovered on this station yet." %}</p>
      {% endfor %}
    </div>
  </div>
{% endblock %}
{% block extra_scripts %}<script src="{% static 'js/control-panel.js' %}" nonce="{{ csp_nonce }}"></script>{% endblock %}
```
Create empty-ish `_lock_banner.html` and `_module_card.html` placeholders (filled in Tasks 2–3) plus an empty `static/css/control-panel.css` and a stub `static/js/control-panel.js` (`document.addEventListener('alpine:init',()=>Alpine.data('controlPanel',()=>({})))`) so the page renders 200 now. Add the station_detail link:

```django
{# apps/stations/templates/stations/station_detail.html — near the terminal/tabs header #}
<a class="btn btn-sm btn-primary" href="{% url 'control:station_control' station.id %}">{% trans "Control panel" %}</a>
```
Gate it on `station.modules.exists` (only show when there is something to control).

- [ ] **Step 6: Run tests — expect PASS.** `pytest tests/test_control_views.py tests/test_control_consumer_relay.py -q`

- [ ] **Step 7: Commit.** `git add -A && git commit -m "feat(control): D5 control page route, access gate, shared inventory snapshot"`

---

## Task 2: Generic descriptor renderer — widget partials

Invoke `Skill("frontend-design")` before writing markup. Build all widget partials + the module card + the dispatch partial. This task proves **generizität**.

**Files:**
- Create widget partials + `_module_card.html`, `widgets/_widget.html` (see File Structure).
- Test: extend `tests/test_control_views.py`.

**Interfaces:**
- Consumes: template context `modules`; each `m` has `.slot`, `.module_id`, `.type/.model/.version`, `.capability_descriptor` (list of caps), `.last_state` (dict), `.online`.
- Produces (widget↔JS contract): each widget root has `data-widget`, `data-slot`, `data-module="{{ m.module_id }}"`, `data-cap="{{ cap.name }}"`, `data-kind`, `data-type`. Interactive elements call `controlPanel` methods with `('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}', …)`.

**Dispatch rules** (`widgets/_widget.html`, an `{% if %}` ladder on `cap.kind`/`cap.type`/`cap.name`):
- `cap.kind == "telemetry"` → `_telemetry.html`
- `cap.kind == "action"` and `cap.name == "ptt"` → `_ptt.html`
- `cap.kind == "action"` → `_action.html`
- `cap.kind == "setting"` and `cap.readonly` → `_text.html` (display-only branch)
- `cap.kind == "setting"` and `cap.type in float,int` → `_number.html`
- `cap.kind == "setting"` and `cap.type == "enum"` → `_enum.html`
- `cap.kind == "setting"` and `cap.type == "bool"` → `_bool.html`
- `cap.kind == "setting"` and `cap.type == "string"` → `_text.html` (editable branch)

**Number widget** (`_number.html`) — the DE-locale-critical one:
```django
{% load i18n %}
<div class="cp-widget cp-number" data-widget data-slot="{{ m.slot }}" data-module="{{ m.module_id }}"
     data-cap="{{ cap.name }}" data-kind="setting" data-type="{{ cap.type }}">
  <label class="cp-widget-label" for="cp-{{ m.slot }}-{{ m.module_id }}-{{ cap.name }}">
    {{ cap.name }}{% if cap.unit %} <span class="cp-unit">{{ cap.unit }}</span>{% endif %}
  </label>
  <div class="cp-number-row">
    <button type="button" class="cp-step" :disabled="!canControl"
            @click="stepValue('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}',-1)">−</button>
    <input id="cp-{{ m.slot }}-{{ m.module_id }}-{{ cap.name }}"
           class="cp-readout" type="number" lang="en" inputmode="decimal"
           {% for r in cap.ranges %}{% if forloop.first %}min="{{ r.min }}"{% endif %}{% endfor %}
           {% for r in cap.ranges %}{% if forloop.last %}max="{{ r.max }}"{% endif %}{% endfor %}
           step="{{ cap.step|default:cap.type|stringformat:'s'|default:'any' }}"
           :value="displayValue('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}')"
           :disabled="!canControl"
           :class="{ 'is-pending': isPending('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}') }"
           @change="setValue('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}', $event.target.value)">
    <button type="button" class="cp-step" :disabled="!canControl"
            @click="stepValue('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}',1)">+</button>
  </div>
  <span class="cp-widget-error" x-show="errorOf('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}')"
        x-text="errorOf('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}')"></span>
</div>
```
> `step` template logic is fiddly; if a computed `step` is awkward in the template, render `data-step`/`data-min`/`data-max`/`data-ranges='{{ cap.ranges|json_script? }}'` and let JS own bounds/step (JS default: `int`→1, `float`→derive from range span or `0.001`, else `"any"`). Keep the DOM contract (`lang="en"`, `inputmode="decimal"`) intact regardless.

The other widgets follow the same skeleton (label + control + error slot). `_enum.html` = `<select>` over `cap.values` bound with `@change="setValue(...)"`. `_bool.html` = a toggle calling `setValue(...,true/false)`. `_action.html` = a button calling `doAction('{{ m.slot }}','{{ m.module_id }}','{{ cap.name }}')`. `_telemetry.html` = a meter/readout: `<span x-text="telemetryText('slot','module','cap')">` + a bar `:style="'width:'+telemetryPct(...)+'%'"` (pct only meaningful when `ranges` present; otherwise show raw value only). `_text.html` = editable text (`setValue`) or, in the readonly branch, `<span x-text="displayValue(...)">`.

`_module_card.html`: header (slot • `m.type` • online dot bound to `moduleOnline('slot','module')`), then loop `for cap in m.capability_descriptor` → `include "control/widgets/_widget.html"`. Group PTT last so it can span full width.

- [ ] **Step 1: Write generizität + widget-mapping tests**

```python
# add to tests/test_control_views.py
GENERIC = [  # a second, fictitious module — NOT fm
    {"name": "azimuth", "kind": "setting", "type": "int", "ranges": [{"min": 0, "max": 359}], "unit": "deg"},
    {"name": "preset", "kind": "setting", "type": "enum", "values": ["park", "north", "zenith"]},
    {"name": "heater", "kind": "setting", "type": "bool"},
    {"name": "calibrate", "kind": "action", "type": "bool"},
    {"name": "temperature", "kind": "telemetry", "type": "int", "readonly": True},
]

def _render(client, station):
    return client.get(reverse("control:station_control", args=[station.pk])).content

def test_second_fictitious_module_renders_without_ui_code(client, station, operator):
    StationModule.objects.create(station=station, slot="slotX", module_id="rotator",
                                 type="rotator", capability_descriptor=GENERIC,
                                 last_state={"azimuth": 90, "preset": "north", "heater": True}, online=True)
    client.force_login(operator)
    html = _render(client, station)
    assert b'data-cap="azimuth"' in html and b'data-type="int"' in html
    assert b'data-cap="preset"' in html          # enum -> select
    assert b'data-cap="heater"' in html           # bool -> toggle
    assert b'data-cap="calibrate"' in html        # action -> button
    assert b'data-cap="temperature"' in html      # telemetry -> meter

def test_renderer_has_no_fm_or_frequency_hardcode():
    import pathlib
    root = pathlib.Path("apps/control/templates/control")
    for p in root.rglob("*.html"):
        txt = p.read_text().lower()
        assert "frequency" not in txt, f"{p} hardcodes 'frequency'"
        assert '"fm"' not in txt and ">fm<" not in txt, f"{p} hardcodes fm"

def test_offline_module_renders_from_last_state(client, station, operator):
    StationModule.objects.create(station=station, slot="slot0", module_id="fm",
                                 type="fm", capability_descriptor=FM,
                                 last_state={"frequency": 145.5}, online=False)
    client.force_login(operator)
    html = _render(client, station).decode()
    assert "145.5" in html            # value present in json_script/SSR
    # offline indicator present
    assert "cp-module" in html

def test_number_input_is_dot_decimal_locale_safe(client, station, operator):
    StationModule.objects.create(station=station, slot="slot0", module_id="fm",
                                 type="fm", capability_descriptor=FM,
                                 last_state={"frequency": 145.5}, online=True)
    client.force_login(operator)
    html = _render(client, station).decode()
    assert 'lang="en"' in html and 'inputmode="decimal"' in html
```

- [ ] **Step 2: Run — expect failure.** `pytest tests/test_control_views.py -q`
- [ ] **Step 3: Implement `_widget.html` dispatch + all widget partials + `_module_card.html`** (frontend-design applied; Task 3 styles them).
- [ ] **Step 4: Run — expect PASS.** `pytest tests/test_control_views.py -q`
- [ ] **Step 5: Commit.** `git commit -m "feat(control): generic descriptor renderer + widget partials (no fm hardcode)"`

---

## Task 3: Control-panel CSS — rack-console styling (frontend-design)

Invoke `Skill("frontend-design")`. Build `static/css/control-panel.css` extending the existing token system (`--accent`, `--signal`, `--danger`, `--bg-*`, `--font-mono`, radii, glow shadows). This is a pure-styling task — no logic; verify visually.

**Deliverables (from the design direction):**
- `.cp-modules` responsive grid of `.cp-module` rack-face cards (radius `--radius-lg`, `--shadow-md`, top label strip with slot/type + online dot).
- `.cp-readout` frequency/number readout: large **IBM Plex Mono** tabular numerals, amber (`--accent`) on `--bg-0`, backlit-dial feel; `.cp-unit` small caption.
- `.cp-step` tactile −/+ keys; `.cp-number-row` groups them.
- `.cp-meter` telemetry bar: track on `--bg-3`, fill `--signal`, amber peak; numeric readout in mono.
- **`.cp-ptt` signature bar:** wide tactile key with three visual states driven by data-attrs/classes the JS toggles — `.is-armed` (accent outline), `.is-keying` (`--warn`, pulsing; `@media (prefers-reduced-motion) → static`), `.is-tx` (`--danger` fill + carrier-glow `box-shadow` bloom, "ON AIR"). `:disabled`/read-only state clearly inert.
- `.cp-lock-banner` states: `.is-you` (`--signal`), `.is-other` (`--warn`), `.is-free` (`--ink-2`); a connection sub-indicator reusing the `pill`/`dot` + `LIVE`/`RECONNECTING` vocabulary.
- Offline/disconnected: `.is-offline` / `.cp-disconnected` desaturate + `--ink-3` + etched "OFFLINE" label; controls visibly inert.
- Quality floor: responsive to mobile (PTT bar stays thumb-reachable), visible keyboard focus rings, reduced-motion honored, both `data-theme` dark/light.

- [ ] **Step 1:** Write the CSS per direction; link is already in `panel.html` `extra_head`.
- [ ] **Step 2:** Load the page against seeded modules (dev server or Playwright screenshot) and self-critique per frontend-design ("remove one accessory"). Verify dark+light, mobile width, focus rings.
- [ ] **Step 3: Commit.** `git commit -m "style(control): rack-console panel styling + PTT transmit-bar signature"`

---

## Task 4: Alpine component + WebSocket client (core)

Invoke `Skill("frontend-design")` for any UI-affecting choices. Build the reactive island + WS client in `static/js/control-panel.js`. PTT + lock detail land in Tasks 5–6; this task delivers connection, inventory/state ingestion, command feedback, telemetry subscription, and connection robustness.

**Interfaces (methods the templates call — keep names stable across tasks):**
- `valueOf(slot,module,cap)` / `displayValue(slot,module,cap)` — current setting value / formatted string.
- `telemetryText(slot,module,cap)` / `telemetryPct(slot,module,cap)` — live telemetry readout + bar %.
- `setValue(slot,module,cap,raw)` — parse dot-decimal, send `command op:set`.
- `stepValue(slot,module,cap,dir)` — bounded ± by step, then `setValue`.
- `doAction(slot,module,cap)` — send `command op:do value:true`.
- `isPending(slot,module,cap)` / `errorOf(slot,module,cap)` — command feedback per widget.
- `moduleOnline(slot,module)` — from inventory `online` flag / events.
- `canControl` (getter) — `lock.you_hold && conn==='open' && agentOnline`.
- PTT (Task 5): `pttDown(slot,module)`, `pttUp(slot,module)`, `pttState(slot,module)`.
- Lock (Task 6): `acquire()`, `release()`, `request()`, `preempt()`, `lockLabel`, `lockClass`.

**Behavior:**
- On `init`: read `#control-initial` json (seed `values`+`telemetry`+`online` from the snapshot so first paint is correct **and offline-safe**); read `data-*` (station id, user id, can-admin, ptt key pref from `localStorage['oe5xrx.ptt_key']` else `data-ptt-default-key`); open WS `ws(s)://host/ws/control/<id>/`.
- Reconnect with capped exponential backoff (mirror app.js: `min(30000, 1000*1.6^retry)`); set `conn ∈ {connecting,open,closed}`; on `open` reset backoff and **re-subscribe** all telemetry.
- Message router by `type`:
  - `inventory` → rebuild module/online maps + seed setting values (do not clobber a locally-pending optimistic edit before its `result`; prefer authoritative server value on `state`).
  - `state` → merge `values` (settings) / `telemetry` (telemetry) by `(slot,module,cap)`; a matching pending clears on the confirming `state`.
  - `result` → clear pending for `request_id`; on `error` set per-widget error text (map `out_of_range`/`bad_value`/etc. to a short human string).
  - `error` → same as result-error (includes `timeout`, `not_locked`).
  - `event` → handle `ptt_auto_unkey` (force local unkey), `module_added`/`module_removed` (mark online/offline; a full re-render needs a fresh `inventory`, which the server sends on topology change), `module_error` (surface).
  - `lock` → store lock (Task 6).
  - `agent_offline` → set `agentOnline=false`, force PTT unkey, disable controls (keep WS/lock chrome live).
- Telemetry subscription: on connect (any viewer), for each module send `subscribe {slot,module,capabilities:[telemetry caps],interval_ms}` using `max(min_interval_ms, UI_RATE)` clamp intent (server also clamps); `unsubscribe` + close WS on `beforeunload`/`destroy`.
- **Send guard:** every browser→server send checks `ws.readyState===OPEN`; if not, no-op (and for PTT, treat as unkey).
- DE-locale: `setValue` parses with `parseFloat(String(raw).replace(',', '.'))` defensively and **serializes dot-decimal**; reject `NaN`.

**Registration (no-inline-JS):**
```js
// static/js/control-panel.js
document.addEventListener("alpine:init", function () {
  Alpine.data("controlPanel", function () {
    return { /* state + methods below */ };
  });
});
```

- [ ] **Step 1: Protocol-level E2E tests (Channels) for command→state + subscribe + feedback.** These drive the real `ControlConsumer` + a simulated agent via `WebsocketCommunicator` (pattern from `tests/test_control_consumer_relay.py`), asserting the *server contract the JS depends on*: a browser `command` reaches the agent; an agent `state`/`result` reaches the browser; `subscribe` relays; a bad command yields a `result.error`/`error` with a structured code. Reuse the `control_agent_auth` fixture.

```python
# tests/test_control_panel_ws.py (sketch — mirror test_control_consumer_relay style)
# 1. browser acquires lock -> command set frequency -> assert agent receives command frame
# 2. agent replies result ok + state values -> assert browser receives both
# 3. browser subscribe -> assert agent receives subscribe frame with capabilities+interval
# 4. non-holder command -> assert browser receives error code "not_locked"
```

- [ ] **Step 2: Run — expect failure** (until any server-side gaps are closed; most already exist in D4 — these tests mainly lock the contract D5 relies on).
- [ ] **Step 3: Implement `control-panel.js` core** (connection, router, values/telemetry, feedback, subscribe). Wire `_number/_enum/_bool/_action/_telemetry` to it.
- [ ] **Step 4: Run — expect PASS.** `pytest tests/test_control_panel_ws.py -q`
- [ ] **Step 5: Commit.** `git commit -m "feat(control): Alpine WS client — inventory/state/subscribe/command feedback + reconnect"`

---

## Task 5: PTT — push-and-hold (mouse/touch + keyboard), guards, confirmed-TX, fail-safe

Invoke `Skill("frontend-design")` for the transmit-bar interaction feel. Extend `control-panel.js` + `_ptt.html`.

**Behavior (spec §5):**
- Two input paths, identical semantics:
  - **Pointer:** `@pointerdown` → key; `@pointerup`/`@pointerleave`/`@pointercancel` → unkey. Use pointer-capture on down so a drag off the button still releases correctly on up.
  - **Keyboard:** configurable key (default Spacebar; stored in `localStorage['oe5xrx.ptt_key']`). `keydown` (matching key) → key; `keyup` → unkey.
- **Key sequence:** key → local state `keying`, send `command {op:"do",capability:"ptt",value:true}`, start **keepalive** interval (`< T_ptt`; use ~1 s) sending `ptt_keepalive {slot,module}`; on confirmation (`state` shows `ptt=true` for that module, or an `event` keyed) flip `keying → tx` ("ON AIR"). Release / leave / blur / WS-drop / lock-loss / `ptt_auto_unkey` → send `command ptt=false`, stop keepalive, state → `armed`.
- **Guards:** only when `canControl`; **ignore key-repeat** (`event.repeat`); **not while typing** (`document.activeElement` is input/select/textarea/`isContentEditable`); `event.preventDefault()` on the PTT key (stop Spacebar page-scroll); one active PTT module at a time (MVP).
- **Confirmed-TX:** the button text/labels read `ARMED` → `KEYING…` (pressed, awaiting agent) → `ON AIR` (agent-confirmed). Operator never assumes TX from the press alone.
- **Dead-man backstop:** local keepalive stop + explicit unkey is primary; the agent dead-man is the safety net (documented in D3 §8). On `window` `blur` and `visibilitychange→hidden`, force unkey.

- [ ] **Step 1: Playwright PTT test (gated on browser availability).**

```python
# tests/e2e/test_control_panel_browser.py
import pytest
playwright = pytest.importorskip("playwright.sync_api")
# Fixture: live ASGI (channels) server + seeded station/module + logged-in session cookie.
# 1. hold pointer on PTT -> assert button shows KEYING…; after simulated agent state ptt=true -> ON AIR
# 2. release -> assert ARMED and a ptt=false command was sent
# 3. keydown Space (not focused in input) -> keys; keydown Space while focused in a number input -> does NOT key
# 4. keydown repeat -> only one key command; blur window -> unkey
# 5. drop WS -> controls disabled + PTT forced to ARMED
```
If a live-server + browser harness is not available in CI, the test `pytest.skip`s with a clear reason; the same behaviors are additionally asserted by pure-logic unit checks below.

- [ ] **Step 2: Pure-logic guard tests (no browser).** Extract the guard predicates into small pure helpers exported for test (e.g. a `pttGuards` object attached to `window.OE5XRX_TEST` in dev, or a tiny separate `static/js/control-ptt-guards.js` module with `shouldIgnoreKey(evt, activeEl, key)` and `nextPttState(prev, signal)`), and test them via a minimal Node runner (`node tests/js/ptt-guards.test.mjs`) invoked from a pytest wrapper (`subprocess`), so key-repeat/typing/state-machine logic is covered even without a browser. Keep the helper import-safe in the browser too.

- [ ] **Step 3: Implement PTT in `control-panel.js` + `_ptt.html`** wiring pointer + keyboard + keepalive + confirmed-TX + guards + fail-safe.
- [ ] **Step 4: Run** `pytest tests/e2e/test_control_panel_browser.py -q` (skips if no browser) **and** the pure-logic runner. Manually verify in a browser: key by mouse and by Spacebar; confirm KEYING…→ON AIR; blur unkeys; typing in the frequency field does not key.
- [ ] **Step 5: Commit.** `git commit -m "feat(control): PTT push-and-hold (pointer+keyboard), guards, confirmed-TX, fail-safe"`

---

## Task 6: Lock UX — banner, hand-off, viewer read-only, loss → read-only + unkey

Invoke `Skill("frontend-design")` for the banner. Extend `control-panel.js` + `_lock_banner.html`.

**Behavior (spec §4):**
- Banner from the `lock` frame: `held` by you → "You have control" (`--signal`); `held` by other → "🔒 {holder} has control" + **Request control** (`lock_request`) (`--warn`); `free` → **Take control** (`lock_acquire`) (`--ink-2`). Holder shows **Release control** (`lock_release`). Admins (`data-can-admin`) additionally see **Override** (`lock_preempt`).
- Non-holder = **read-only**: all setting/action/PTT controls `:disabled` (`canControl` false); live values + telemetry still update.
- `control_requested` frame (holder only) → non-blocking prompt "{requester} is requesting control" with Grant (`lock_transfer to_user_id=requester.id`) / dismiss.
- **Lock loss while operating** (preempt or idle sweep → a `lock` frame where `you_hold` flips false): immediately **force PTT unkey**, switch to read-only, show a transient "You no longer have control" notice.
- All hand-off actions go over the control WS (D4 handles enforcement/audit).

- [ ] **Step 1: Channels tests for lock hand-off + loss.** Extend `tests/test_control_panel_ws.py`: browser A acquires → B sees `held`+`you_hold:false`; B `lock_request` → A receives `control_requested`; admin `lock_preempt` → A receives `lock` with `you_hold:false` (the JS unkey trigger); non-admin preempt → `error forbidden`.
- [ ] **Step 2: Run — expect pass at the server contract** (D4 implements this; these lock the shape the JS keys off). Add any missing assertions.
- [ ] **Step 3: Implement lock UI + loss→unkey in `control-panel.js` + `_lock_banner.html`.** The loss→unkey path reuses Task 5's fail-safe unkey.
- [ ] **Step 4: Playwright/manual: two sessions** — acquire in A, verify B read-only + live values; preempt/idle in A while keyed → A goes read-only and unkeys.
- [ ] **Step 5: Commit.** `git commit -m "feat(control): lock UX — banner, hand-off, viewer read-only, loss→read-only+unkey"`

---

## Task 7: Connection/offline robustness + end-to-end verification against native_sim + Agent + D4

**Behavior (spec §8):**
- WS disconnect/reconnect indicator prominent (reuse `LIVE`/`RECONNECTING`); on drop **all controls disabled** and PTT forced `armed` (fail-safe); reconnect backoff; on reconnect re-subscribe + the server's fresh `inventory`/`lock` frames re-sync.
- Offline station/module (agent offline): read-only + "offline" indicator, render from `last_state`; no lock/command attempts (or they surface a clear error).

- [ ] **Step 1: Channels test — agent disconnect.** Browser connected + holder; disconnect the agent communicator → assert browser receives `agent_offline` and (from D4 `force_free`) a `lock` free frame. Assert this is the JS trigger to disable + unkey.
- [ ] **Step 2: Reconnect/offline render tests.** Panel with an offline module renders values from `last_state` (already in Task 2); add a JS-level (Playwright, gated) drop→disabled→reconnect→enabled check.
- [ ] **Step 3: True E2E against the sim.** Bring up the D2 `native_sim` module + the `station_agent` broker + D4 server locally; open the panel; verify: set a setting (command→result→state reflected), subscribe telemetry (live meter), PTT key (KEYING…→ON AIR confirmed, keepalive flowing, release unkeys), lock hand-off between two browsers. Capture the steps in the PR description (this is the DoD demonstration). Reference the D2 sim harness (`linux-image/docs/sim-station.md`) for launch.
- [ ] **Step 4:** Run the full suite: `pytest -q` + `ruff check . && ruff format --check .`. All green.
- [ ] **Step 5: Commit.** `git commit -m "feat(control): connection/offline robustness + E2E verification"`

---

## Task 8: Finalize — verification, PR, copilot-loop

- [ ] **Step 1:** `superpowers:verification-before-completion` — run `pytest -q`, `ruff check . && ruff format --check .`, and load the page manually; paste real command output. No success claim without evidence.
- [ ] **Step 2:** Self-review vs spec §10 DoD checklist:
  - [ ] Generic render, no `fm` hardcode; second fictitious module appears with no UI code.
  - [ ] Control (command→state), PTT confirmed-TX (mouse+keyboard), live telemetry — E2E vs native_sim + D4.
  - [ ] Full lock UX incl. loss→read-only+unkey; non-holder read-only.
  - [ ] WS-drop → controls disabled + PTT fail-safe; offline render from `last_state`; frequency DE-locale-safe.
  - [ ] frontend-design quality; CI green.
- [ ] **Step 3:** Push branch; open PR with `Closes #99`, the E2E demonstration notes, and the two-session lock demo.
- [ ] **Step 4:** Run the `copilot-loop` skill (4 min initial wait, 1 min poll, 10 min total; Opus for code-quality). station-manager PRs typically need several rounds — address each.

---

## Self-Review (author pass)

- **Spec coverage:** §2 route/renderer/Alpine/WS → Tasks 1–4; §3 widget mapping → Task 2; §3 frequency DE-locale → Tasks 2+4; §4 lock UX+loss → Task 6; §5 PTT (both inputs, guards, confirmed-TX, dead-man) → Task 5; §6 telemetry subscribe → Task 4; §7 command feedback → Task 4; §8 connection/offline → Task 7; §10 testing → Tasks 2,4,5,6,7. All covered.
- **Placeholders:** none — code/interfaces are concrete; the two soft spots (number `step` derivation, JS-in-CI test harness) are flagged with an explicit fallback (JS owns bounds via data-attrs; pure-logic Node runner when no browser). Not left as "TBD".
- **Type/name consistency:** `snapshot()`, `canControl`, `setValue/stepValue/doAction`, `valueOf/displayValue`, `telemetryText/telemetryPct`, `isPending/errorOf`, `moduleOnline`, `pttDown/pttUp/pttState`, `acquire/release/request/preempt` used consistently across tasks. Widget↔JS data-attr contract (`data-slot/module/cap/kind/type`) is uniform. `control:station_control` URL name stable.
- **Scope guard:** no voice/audio (D6–D9); no per-module lock / presence (#97) / skins — deferred per spec §9.
