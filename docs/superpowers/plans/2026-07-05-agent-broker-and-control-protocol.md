# D3 — Agent-Broker & Control-Protokoll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the station_agent's D2 module discovery into a generic **broker** that receives semantic commands over a dedicated persistent Ed25519 Control-WS, validates them against the cached descriptor, translates them into the generic firmware syntax, executes them over the slot-control channel, and reports result / actual-state / telemetry back — end-to-end against `native_sim`, no hardware.

**Architecture:** A device-agnostic broker sits between a persistent outbound Control-WebSocket (`ControlClient`, mirroring the proven Ed25519 pattern from `terminal.py`) and the concrete slot-control serial/shell transport (`SlotControl`, the "SA818 driver" layer — the *only* place touching real device I/O). The broker addresses everything generically as `(slot, module, capability)` resolved through cached descriptors from D2 `describe`; it never hardcodes a module id like `fm`. Validation (type/range/enum/kind/op) happens **before** the firmware; telemetry is subscription-driven and rate-clamped to a descriptor `min_interval`; PTT has an agent-local dead-man that unkeys on keepalive-timeout **or** WS-disconnect.

**Tech Stack:** Python 3.10+ (station_agent), `websockets` (already a dep), `cryptography` Ed25519 (already a dep), stdlib `asyncio`/`selectors`/`pty`. Tests: `pytest` with `asyncio.run()` (no `pytest-asyncio` — matches existing `test_terminal_agent.py`); firmware simulated via a pty fake-FW harness extended from `tests/test_slot_discovery.py`.

## Global Constraints

- **No `"fm"` (or any module id) hardcoded in the broker.** Module ids come only from D2 `module list` → `describe`. The generality test (a second fictitious module flowing through unchanged) is a DoD gate.
- **Validate before the firmware.** Existence / type / range / enum / kind / op are checked against the cached descriptor and rejected with a structured error *before* any bytes reach the slot. (Spec §12.)
- **Envelope `{ "v": 1, "type": …, … }`** on every message. Protocol version `v` and descriptor `schema` are independent. (Spec §7/§10.)
- **Structured errors `error: {code, msg}`.** Codes: `validation_failed`, `unknown_slot`, `unknown_module`, `unknown_capability`, `wrong_op`, `read_only`, `out_of_range`, `bad_value`, `timeout`. FW error strings from `MODULE-RESULT` pass through verbatim as the `code`. (Spec §10.)
- **No polling without a subscriber.** Idle telemetry = silent. Subscription rate is clamped to `max(requested, min_interval)`; `min_interval` comes from the telemetry capability's descriptor if present, else a safe config default. (Spec §6.)
- **PTT dead-man is agent-local and fail-safe.** `ptt` (kind=action) TX starts a timer; a missed `ptt_keepalive` within `T` **or** a Control-WS disconnect unkeys locally and emits `event: ptt_auto_unkey`. `T` conservative (~1.5 s default). (Spec §8.)
- **Reuse `terminal.py`'s outbound Ed25519 WS pattern** (timestamp + body-hash signature query params, exponential reconnect backoff). New path: `/ws/agent/control/<station_id>/`. Persistent while online (not on-demand like terminal). (Spec §9.)
- **Server side is OUT of scope (D4).** `ControlClient` is tested against a mock `websockets` server, not the real `ControlConsumer`.
- Follow existing station_agent style: module-level `logger = logging.getLogger(__name__)`, guarded I/O that never raises into the loop, `from __future__ import annotations` where helpful.

---

## Firmware contract reference (already implemented in FW-RemoteStation, verified)

Descriptor from `module <id> describe` → `MODULE-DESCRIBE <json>`:
```json
{"schema":1,"module":"fm","identity":{"type":"fm_transceiver","model":"SA818-V","version":"vhf"},
 "capabilities":[
   {"name":"frequency","kind":"setting","type":"float","unit":"MHz","ranges":[{"name":"vhf","min":134.0,"max":174.0}],"access":"operator"},
   {"name":"ptt","kind":"action","type":"bool","access":"operator"},
   {"name":"power_level","kind":"setting","type":"enum","values":["low","high"],"access":"operator"},
   {"name":"rssi","kind":"telemetry","type":"int","unit":"raw","readonly":true,"access":"operator"},
   {"name":"volume","kind":"setting","type":"int","ranges":[{"min":1,"max":8}],"access":"operator"}]}
```
Command `module <id> <op> <cap> [token]` → `MODULE-RESULT <json>`:
```json
{"ok":true,"module":"fm","cap":"frequency","op":"set","value":145.5}
{"ok":false,"module":"fm","cap":"frequency","op":"set","error":"out_of_range"}
```
- **Op↔kind gating (mirror in broker, reject before FW):** `setting`→`{set,get}`; `action`→`{do,get}`; `telemetry`→`{get}` (a `set` to telemetry is `read_only`).
- **FW value tokens:** bool accepts `true`/`false`/`on`/`off`/`1`/`0` (`sa818_module.cpp:72-75`). Broker emits canonical `"true"`/`"false"`. Int→`"7"`. Float→`"145.5"`. Enum/String→the value verbatim (e.g. `"high"`, `"12.5"`, `"none"`, `"67.0"`).
- **`min_interval`** is NOT yet in the descriptor (companion FW-RemoteStation #52). Broker reads `min_interval_ms` from the telemetry capability if present, else falls back to the config default. No hard dependency on the FW change.

---

## File Structure

New files in `station_agent/`:
- `protocol.py` — envelope + message builders + error codes + `ProtocolError`. Pure, no I/O.
- `descriptor.py` — descriptor indexing, command validation (`validate_command`), value formatting (`format_value`), `min_interval_ms`. Pure, no I/O.
- `slot_control.py` — the concrete serial/shell transport (the spec's "SA818 driver" layer): open a slot-control path, send one `module <id> <op> <cap> [token]`, read the `MODULE-RESULT` line. Device I/O lives here and *only* here.
- `broker.py` — the device-agnostic `Broker`: inventory cache, command pipeline, subscription telemetry, PTT dead-man. Async; injected `send` + `transport_factory`.
- `control_client.py` — `ControlClient`: persistent outbound Ed25519 Control-WS, reconnect, routes server→broker and broker→ws, discovery + inventory on connect, disconnect→dead-man.

New test files in `tests/`:
- `test_protocol.py`, `test_descriptor.py`, `test_slot_control.py`, `test_broker.py`, `test_control_client.py`, `test_broker_generic.py` (generality DoD gate).
- `tests/fake_fw.py` — shared pty fake-firmware harness (stateful: list/describe/set/get/do + MODULE-RESULT), extracted and extended from `test_slot_discovery.py`'s `_fake_firmware`.

Modified files:
- `station_agent/config.py` — add `control_enabled`, `control_dead_man_timeout`, `telemetry_default_interval_ms`, `telemetry_min_floor_ms`.
- `station_agent/agent.py` — start `ControlClient` in a background thread (mirrors `TerminalClient` wiring).
- `station_agent/config.example.yml` — document the new keys.

---

## Task 1: Protocol envelope, message builders, error codes

**Files:**
- Create: `station_agent/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Produces:
  - `PROTOCOL_VERSION: int = 1`
  - Error-code constants: `VALIDATION_FAILED`, `UNKNOWN_SLOT`, `UNKNOWN_MODULE`, `UNKNOWN_CAPABILITY`, `WRONG_OP`, `READ_ONLY`, `OUT_OF_RANGE`, `BAD_VALUE`, `TIMEOUT` (all `str`).
  - `class ProtocolError(Exception)` with `.code: str`, `.msg: str`.
  - `build_result(request_id, ok, value=None, error=None) -> dict`
  - `build_state(slot, module, values, ts) -> dict`
  - `build_event(slot, module, event, detail) -> dict`
  - `build_inventory(slots) -> dict`
  - `parse_message(text) -> dict` (raises `ProtocolError(VALIDATION_FAILED, …)` on non-JSON / non-dict / missing `type`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_protocol.py
import json
import pytest
from station_agent import protocol as p


def test_build_result_ok_carries_value_and_version():
    msg = p.build_result("req-1", True, value=145.5)
    assert msg == {"v": 1, "type": "result", "request_id": "req-1", "ok": True, "value": 145.5}


def test_build_result_error_carries_structured_error():
    msg = p.build_result("req-2", False, error=(p.OUT_OF_RANGE, "200.0 not in range"))
    assert msg["ok"] is False
    assert msg["error"] == {"code": "out_of_range", "msg": "200.0 not in range"}
    assert "value" not in msg


def test_build_state_shape():
    msg = p.build_state(1, "fm", {"frequency": 145.5}, ts=1234.0)
    assert msg == {"v": 1, "type": "state", "slot": 1, "module": "fm",
                   "values": {"frequency": 145.5}, "ts": 1234.0}


def test_build_event_shape():
    msg = p.build_event(1, "fm", "ptt_auto_unkey", {"reason": "keepalive_timeout"})
    assert msg["type"] == "event" and msg["event"] == "ptt_auto_unkey"
    assert msg["detail"] == {"reason": "keepalive_timeout"}


def test_build_inventory_wraps_slots():
    slots = [{"slot": 1, "modules": []}]
    msg = p.build_inventory(slots)
    assert msg["v"] == 1 and msg["type"] == "inventory" and msg["slots"] == slots


def test_parse_message_rejects_non_json():
    with pytest.raises(p.ProtocolError) as exc:
        p.parse_message("not json{")
    assert exc.value.code == p.VALIDATION_FAILED


def test_parse_message_rejects_missing_type():
    with pytest.raises(p.ProtocolError):
        p.parse_message(json.dumps({"v": 1}))


def test_parse_message_accepts_valid():
    out = p.parse_message(json.dumps({"v": 1, "type": "command", "request_id": "x"}))
    assert out["type"] == "command"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: station_agent.protocol` / `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

```python
# station_agent/protocol.py
"""Agent<->Server control-protocol envelope, message builders, and error codes.

Pure data helpers — no I/O. The envelope is ``{"v": PROTOCOL_VERSION, "type": ...}``
on every message (design spec §7). Protocol version ``v`` and descriptor ``schema``
version are independent (§10).
"""

from __future__ import annotations

import json

PROTOCOL_VERSION = 1

# Structured error codes (§10). FW MODULE-RESULT error strings pass through as-is.
VALIDATION_FAILED = "validation_failed"
UNKNOWN_SLOT = "unknown_slot"
UNKNOWN_MODULE = "unknown_module"
UNKNOWN_CAPABILITY = "unknown_capability"
WRONG_OP = "wrong_op"
READ_ONLY = "read_only"
OUT_OF_RANGE = "out_of_range"
BAD_VALUE = "bad_value"
TIMEOUT = "timeout"


class ProtocolError(Exception):
    """A structured protocol/validation error carrying a code + human message."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code
        self.msg = msg


def _envelope(msg_type: str, **fields) -> dict:
    return {"v": PROTOCOL_VERSION, "type": msg_type, **fields}


def build_result(request_id, ok: bool, value=None, error=None) -> dict:
    """Build a ``result`` reply. On error, ``error`` is a (code, msg) tuple."""
    msg = _envelope("result", request_id=request_id, ok=bool(ok))
    if ok:
        msg["value"] = value
    else:
        code, emsg = error if error is not None else (VALIDATION_FAILED, "")
        msg["error"] = {"code": code, "msg": emsg}
    return msg


def build_state(slot, module, values: dict, ts: float) -> dict:
    return _envelope("state", slot=slot, module=module, values=values, ts=ts)


def build_event(slot, module, event: str, detail) -> dict:
    return _envelope("event", slot=slot, module=module, event=event, detail=detail)


def build_inventory(slots: list) -> dict:
    return _envelope("inventory", slots=slots)


def parse_message(text: str) -> dict:
    """Parse an inbound frame; raise ProtocolError(VALIDATION_FAILED) if malformed."""
    try:
        msg = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise ProtocolError(VALIDATION_FAILED, "message is not valid JSON")
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError(VALIDATION_FAILED, "message missing 'type'")
    return msg
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_protocol.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add station_agent/protocol.py tests/test_protocol.py
git commit -m "feat(agent): control-protocol envelope, message builders, error codes"
```

---

## Task 2: Descriptor indexing, validation, and value formatting

**Files:**
- Create: `station_agent/descriptor.py`
- Test: `tests/test_descriptor.py`

**Interfaces:**
- Consumes: `station_agent.protocol` error codes + `ProtocolError`.
- Produces:
  - `index_capabilities(module_descriptor: dict) -> dict[str, dict]` — name→cap descriptor.
  - `validate_command(cap: dict | None, op: str, value) -> None` — raises `ProtocolError` with the right code (`unknown_capability`, `wrong_op`, `read_only`, `bad_value`, `out_of_range`) before the FW ever sees the command.
  - `format_value(cap_type: str, value) -> str` — canonical FW token for a validated value.
  - `min_interval_ms(cap: dict, default: int) -> int` — descriptor `min_interval_ms` clamped to ≥ `default` floor's caller; returns descriptor value if present & positive, else `default`.

Design notes for the implementer:
- Op↔kind gating: `setting`→`{set,get}`, `action`→`{do,get}`, `telemetry`→`{get}`. A write op (`set`/`do`) to a `readonly` cap or to `telemetry` → `read_only`. An op not allowed for the kind → `wrong_op`.
- `get` never carries a value; `set` always requires a value; `do` requires a value (our only action, `ptt`, is bool and needs one — a no-arg action is out of scope for D3).
- Type checks (value is the JSON-decoded Python value): `bool`→`isinstance(value, bool)`; `int`→`isinstance(value, int) and not isinstance(value, bool)`; `float`→`isinstance(value, (int, float)) and not bool`; `enum`→`value in cap["values"]`; `string`→`isinstance(value, str)`. Type mismatch → `bad_value`.
- Range check (only for `int`/`float` with a `ranges` list): value must fall in at least one range `[min,max]`, else `out_of_range`. No ranges → no numeric bound (FW still guards).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_descriptor.py
import pytest
from station_agent import descriptor as d
from station_agent import protocol as p

FM = {
    "schema": 1, "module": "fm",
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float",
         "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}]},
        {"name": "volume", "kind": "setting", "type": "int", "ranges": [{"min": 1, "max": 8}]},
        {"name": "power_level", "kind": "setting", "type": "enum", "values": ["low", "high"]},
        {"name": "ptt", "kind": "action", "type": "bool"},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 250},
        {"name": "band", "kind": "telemetry", "type": "string", "readonly": True},
    ],
}


def caps():
    return d.index_capabilities(FM)


def test_index_capabilities_by_name():
    idx = caps()
    assert set(idx) == {"frequency", "volume", "power_level", "ptt", "rssi", "band"}
    assert idx["frequency"]["type"] == "float"


def test_unknown_capability_rejected():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(None, "set", 145.5)
    assert e.value.code == p.UNKNOWN_CAPABILITY


def test_setting_accepts_set_and_get():
    d.validate_command(caps()["frequency"], "set", 145.5)  # no raise
    d.validate_command(caps()["frequency"], "get", None)   # no raise


def test_setting_rejects_do_as_wrong_op():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["frequency"], "do", 145.5)
    assert e.value.code == p.WRONG_OP


def test_action_accepts_do_and_get_but_not_set():
    d.validate_command(caps()["ptt"], "do", True)
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["ptt"], "set", True)
    assert e.value.code == p.WRONG_OP


def test_telemetry_get_ok_set_is_read_only():
    d.validate_command(caps()["rssi"], "get", None)
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["rssi"], "set", 5)
    assert e.value.code == p.READ_ONLY


def test_float_out_of_range_rejected_before_fw():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["frequency"], "set", 200.0)
    assert e.value.code == p.OUT_OF_RANGE


def test_int_out_of_range_rejected():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["volume"], "set", 9)
    assert e.value.code == p.OUT_OF_RANGE


def test_enum_not_in_values_rejected():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["power_level"], "set", "medium")
    assert e.value.code == p.BAD_VALUE


def test_bool_type_mismatch_rejected():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["ptt"], "do", "on")  # string, not bool
    assert e.value.code == p.BAD_VALUE


def test_int_rejects_bool_value():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["volume"], "set", True)
    assert e.value.code == p.BAD_VALUE


def test_set_requires_value():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["frequency"], "set", None)
    assert e.value.code == p.BAD_VALUE


def test_format_value_canonical_tokens():
    assert d.format_value("bool", True) == "true"
    assert d.format_value("bool", False) == "false"
    assert d.format_value("int", 7) == "7"
    assert d.format_value("float", 145.5) == "145.5"
    assert d.format_value("float", 146.0) == "146.0"
    assert d.format_value("enum", "high") == "high"
    assert d.format_value("string", "67.0") == "67.0"


def test_min_interval_from_descriptor_else_default():
    assert d.min_interval_ms(caps()["rssi"], default=100) == 250
    assert d.min_interval_ms(caps()["band"], default=100) == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_descriptor.py -v`
Expected: FAIL (`ModuleNotFoundError: station_agent.descriptor`).

- [ ] **Step 3: Write minimal implementation**

```python
# station_agent/descriptor.py
"""Descriptor-driven command validation + value formatting.

Pure, no I/O. The broker validates every command against the cached ``describe``
descriptor *before* the firmware (spec §12): existence, kind<->op gating, type,
range, and enum are all checked here. Value formatting turns a validated JSON value
into the canonical firmware token. Adding a module / capability / value type needs
NO change here — it is all read from the descriptor.
"""

from __future__ import annotations

from station_agent.protocol import (
    BAD_VALUE,
    OUT_OF_RANGE,
    READ_ONLY,
    UNKNOWN_CAPABILITY,
    WRONG_OP,
    ProtocolError,
)

# kind -> the set of ops that kind accepts (mirrors FW iface.h mixins).
_OPS_FOR_KIND = {
    "setting": {"set", "get"},
    "action": {"do", "get"},
    "telemetry": {"get"},
}
_WRITE_OPS = {"set", "do"}


def index_capabilities(module_descriptor: dict) -> dict:
    """Return {capability_name: descriptor_dict} for a module's ``describe`` output."""
    out = {}
    for cap in module_descriptor.get("capabilities", []):
        name = cap.get("name")
        if isinstance(name, str):
            out[name] = cap
    return out


def validate_command(cap: dict | None, op: str, value) -> None:
    """Raise ProtocolError if (cap, op, value) is invalid. Return None if valid."""
    if cap is None:
        raise ProtocolError(UNKNOWN_CAPABILITY, "no such capability")

    kind = cap.get("kind", "")
    allowed = _OPS_FOR_KIND.get(kind, set())

    # A write to telemetry or a readonly cap is read_only; other bad ops are wrong_op.
    if op in _WRITE_OPS and (kind == "telemetry" or cap.get("readonly")):
        raise ProtocolError(READ_ONLY, f"{cap.get('name')} is read-only")
    if op not in allowed:
        raise ProtocolError(WRONG_OP, f"op {op!r} not valid for kind {kind!r}")

    if op == "get":
        return  # get never carries a value

    if value is None:
        raise ProtocolError(BAD_VALUE, "value required")

    _check_value(cap, value)


def _check_value(cap: dict, value) -> None:
    vtype = cap.get("type")
    if vtype == "bool":
        if not isinstance(value, bool):
            raise ProtocolError(BAD_VALUE, "expected bool")
        return
    if vtype == "enum":
        if value not in cap.get("values", []):
            raise ProtocolError(BAD_VALUE, f"{value!r} not an enum value")
        return
    if vtype == "string":
        if not isinstance(value, str):
            raise ProtocolError(BAD_VALUE, "expected string")
        return
    if vtype == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProtocolError(BAD_VALUE, "expected int")
        _check_range(cap, value)
        return
    if vtype == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError(BAD_VALUE, "expected number")
        _check_range(cap, float(value))
        return
    raise ProtocolError(BAD_VALUE, f"unknown value type {vtype!r}")


def _check_range(cap: dict, value) -> None:
    ranges = cap.get("ranges")
    if not ranges:
        return
    for r in ranges:
        if r.get("min", float("-inf")) <= value <= r.get("max", float("inf")):
            return
    raise ProtocolError(OUT_OF_RANGE, f"{value} outside all ranges")


def format_value(cap_type: str, value) -> str:
    """Canonical firmware token for a validated value."""
    if cap_type == "bool":
        return "true" if value else "false"
    if cap_type == "int":
        return str(int(value))
    if cap_type == "float":
        # Always-decimal, no exponent; matches the FW's whole-string float parse.
        text = repr(float(value))
        return text
    # enum / string are passed through verbatim.
    return str(value)


def min_interval_ms(cap: dict, default: int) -> int:
    """Descriptor-declared min_interval_ms if present & positive, else the default."""
    val = cap.get("min_interval_ms")
    if isinstance(val, int) and not isinstance(val, bool) and val > 0:
        return val
    return default
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_descriptor.py -v`
Expected: PASS. Note: `repr(146.0) == "146.0"` and `repr(145.5) == "145.5"` — verify these two assertions pass; if a value like `repr(145.500) == "145.5"` differs from expectation, the FW parses `"145.5"` fine (`test_module_frequency_serializes_as_float`), so the assertion is correct.

- [ ] **Step 5: Commit**

```bash
git add station_agent/descriptor.py tests/test_descriptor.py
git commit -m "feat(agent): descriptor-driven command validation + value formatting"
```

---

## Task 3: Shared pty fake-firmware harness

**Files:**
- Create: `tests/fake_fw.py`
- Test: `tests/test_fake_fw.py` (self-test of the harness — a harness this central earns its own gate)

**Interfaces:**
- Produces:
  - `class FakeFirmware` — a stateful pty peer. Constructor `FakeFirmware(modules: dict[str, dict])` where each value is a `describe` dict. Public: `.control_path` (a filesystem path usable as a slot `control`), `.start()`, `.stop()`, `.state: dict[str, dict]` (per-module capability values for assertions).
  - Responds to `module list`, `module <id> describe`, and `module <id> <op> <cap> [token]` with `MODULE-LIST` / `MODULE-DESCRIBE` / `MODULE-RESULT` lines. `set`/`do` update `.state`; `get` reads it (telemetry like `rssi` returns a canned value). Unknown module/capability → `MODULE-RESULT` error.
  - `make_slot_tree(tmp_path, {slot_number: FakeFirmware}) -> str` — builds a `slotN/control` symlink tree and returns the base dir for `discover_slots`.

Implementer guidance: lift the pty + `_fake_firmware` thread from `tests/test_slot_discovery.py`, generalize the command matcher to also handle `set|get|do`, and keep a per-module value dict. Reuse the exact `MODULE-RESULT` shape from `test_module_iface.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fake_fw.py
import os
from station_agent import slot_discovery
from tests.fake_fw import FakeFirmware, make_slot_tree

FM = {
    "schema": 1, "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float",
         "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}]},
        {"name": "ptt", "kind": "action", "type": "bool"},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True},
    ],
}


def _send(path, line):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    try:
        os.write(fd, (line + "\r\n").encode())
        import select
        buf = b""
        while b"MODULE-RESULT" not in buf and b"MODULE-DESCRIBE" not in buf and b"MODULE-LIST" not in buf:
            r, _, _ = select.select([fd], [], [], 2.0)
            if not r:
                break
            buf += os.read(fd, 4096)
        return buf.decode(errors="replace")
    finally:
        os.close(fd)


def test_fake_fw_lists_and_describes(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        assert "MODULE-LIST" in _send(fw.control_path, "module list")
        assert "fm_transceiver" in _send(fw.control_path, "module fm describe")
    finally:
        fw.stop()


def test_fake_fw_set_updates_state_and_get_reads_it(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        out = _send(fw.control_path, "module fm set frequency 145.5")
        assert '"ok":true' in out.replace(" ", "")
        assert fw.state["fm"]["frequency"] == "145.5"
        got = _send(fw.control_path, "module fm get frequency")
        assert "145.5" in got
    finally:
        fw.stop()


def test_make_slot_tree_discoverable(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw})
        slots = slot_discovery.discover_slots(base, timeout=3.0)
        assert slots and slots[0]["slot"] == 1
        assert slots[0]["modules"][0]["id"] == "fm"
    finally:
        fw.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_fake_fw.py -v`
Expected: FAIL (`ModuleNotFoundError: tests.fake_fw`).

- [ ] **Step 3: Write minimal implementation**

```python
# tests/fake_fw.py
"""Stateful pty fake-firmware harness for broker end-to-end tests.

Speaks the same self-describing shell protocol as the real firmware on native_sim:
``module list`` -> MODULE-LIST, ``module <id> describe`` -> MODULE-DESCRIBE, and
``module <id> <op> <cap> [token]`` -> MODULE-RESULT. set/do mutate per-module state;
get reads it. This is the transport under broker E2E tests — no hardware, no server.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

_LIST_RE = re.compile(rb"module\s+list\s*$")
_DESCRIBE_RE = re.compile(rb"module\s+(\S+)\s+describe\s*$")
_CMD_RE = re.compile(rb"module\s+(\S+)\s+(set|get|do)\s+(\S+)(?:\s+(\S+))?\s*$")


class FakeFirmware:
    def __init__(self, modules: dict):
        self._modules = modules
        self.state: dict = {mid: {} for mid in modules}
        self._master_fd, self._slave_fd = os.openpty()
        self.control_path = os.ttyname(self._slave_fd)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        try:
            os.close(self._slave_fd)
        except OSError:
            pass
        self._thread.join(timeout=1)

    def _w(self, s: str):
        try:
            os.write(self._master_fd, s.encode())
        except (BlockingIOError, OSError):
            pass

    def _result(self, mid, cap, op, ok, value=None, error=None):
        body = {"ok": ok, "module": mid, "cap": cap, "op": op}
        if ok:
            body["value"] = value
        else:
            body["error"] = error
        self._w("MODULE-RESULT " + json.dumps(body) + "\r\n")

    def _cap(self, mid, cap):
        for c in self._modules.get(mid, {}).get("capabilities", []):
            if c.get("name") == cap:
                return c
        return None

    def _handle_cmd(self, mid, op, cap, token):
        if mid not in self._modules:
            self._result(mid, cap, op, False, error="unknown_module")
            return
        c = self._cap(mid, cap)
        if c is None:
            self._result(mid, cap, op, False, error="unknown_capability")
            return
        if op == "get":
            # Telemetry returns a canned reading; settings echo last-set value.
            if c.get("kind") == "telemetry":
                val = 42 if c.get("type") == "int" else "vhf"
            else:
                val = self.state[mid].get(cap)
            self._result(mid, cap, op, True, value=val)
            return
        # set / do: store the raw token (tests assert on it) and echo it back.
        self.state[mid][cap] = token
        self._result(mid, cap, op, True, value=token)

    def _serve(self):
        os.set_blocking(self._master_fd, False)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(self._master_fd, 1024)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if _LIST_RE.search(line):
                    self._w("MODULE-LIST " + json.dumps({"modules": list(self._modules)}) + "\r\n")
                    continue
                m = _DESCRIBE_RE.search(line)
                if m:
                    mid = m.group(1).decode(errors="replace")
                    spec = self._modules.get(mid)
                    if spec is not None:
                        self._w("MODULE-DESCRIBE " + json.dumps(spec) + "\r\n")
                    else:
                        self._result(mid, "", "describe", False, error="unknown_module")
                    continue
                m = _CMD_RE.search(line)
                if m:
                    mid = m.group(1).decode(errors="replace")
                    op = m.group(2).decode()
                    cap = m.group(3).decode(errors="replace")
                    token = m.group(4).decode(errors="replace") if m.group(4) else None
                    self._handle_cmd(mid, op, cap, token)


def make_slot_tree(tmp_path, slots: dict) -> str:
    """Build a slotN/control symlink tree under tmp_path; return the base dir."""
    base = tmp_path / "oe5xrx"
    base.mkdir(exist_ok=True)
    for num, fw in slots.items():
        slot_dir = base / f"slot{num}"
        slot_dir.mkdir(exist_ok=True)
        (slot_dir / "control").symlink_to(fw.control_path)
    return str(base)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_fake_fw.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/fake_fw.py tests/test_fake_fw.py
git commit -m "test(agent): stateful pty fake-firmware harness for broker E2E"
```

---

## Task 4: Slot-control transport (the concrete device driver layer)

**Files:**
- Create: `station_agent/slot_control.py`
- Test: `tests/test_slot_control.py`

**Interfaces:**
- Consumes: `station_agent.slot_discovery._extract_json` (reuse the exact MODULE-RESULT/line parser).
- Produces:
  - `class SlotControl` — `__init__(control_path: str, timeout: float = 3.0)`.
  - `execute(module_id: str, op: str, cap: str, token: str | None = None) -> dict` — writes `module <id> <op> <cap> [token]`, reads the first `MODULE-RESULT` line, returns the parsed dict. On open/write/read error or timeout returns `{"ok": False, "error": "timeout"}`. Never raises. Opens/closes the control fd per call (fast; keeps line discipline clean; matches D2's `probe_slot`).

Implementer guidance: model the read loop on `slot_discovery._command` (select + deadline + byte cap + `_extract_json` with prefix `"MODULE-RESULT "`). Validate `module_id`/`cap` against `slot_discovery._MODULE_ID_RE`-style safe tokens before echoing into the shell (defense-in-depth; the broker also validates, but the transport must never inject control bytes).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_slot_control.py
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

FM = {
    "schema": 1, "module": "fm",
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float"},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True},
    ],
}


def test_execute_set_returns_module_result():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        sc = SlotControl(fw.control_path, timeout=2.0)
        r = sc.execute("fm", "set", "frequency", "145.5")
        assert r["ok"] is True
        assert r["cap"] == "frequency" and r["op"] == "set"
        assert fw.state["fm"]["frequency"] == "145.5"
    finally:
        fw.stop()


def test_execute_get_reads_value():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        sc = SlotControl(fw.control_path, timeout=2.0)
        r = sc.execute("fm", "get", "rssi")
        assert r["ok"] is True and isinstance(r["value"], int)
    finally:
        fw.stop()


def test_execute_unknown_capability_passes_through_fw_error():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        sc = SlotControl(fw.control_path, timeout=2.0)
        r = sc.execute("fm", "set", "banana", "1")
        assert r["ok"] is False and r["error"] == "unknown_capability"
    finally:
        fw.stop()


def test_execute_timeout_on_dead_path(tmp_path):
    missing = str(tmp_path / "control")
    sc = SlotControl(missing, timeout=0.3)
    r = sc.execute("fm", "get", "rssi")
    assert r["ok"] is False and r["error"] == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_slot_control.py -v`
Expected: FAIL (`ModuleNotFoundError: station_agent.slot_control`).

- [ ] **Step 3: Write minimal implementation**

```python
# station_agent/slot_control.py
"""Concrete slot-control serial/shell transport — the device I/O layer.

This is the only station_agent module that talks to a slot's ``control`` device.
It writes one generic ``module <id> <op> <cap> [token]`` command and reads the
firmware's ``MODULE-RESULT`` reply. The broker above it is device-agnostic; all
serial/shell specifics (line discipline, byte cap, timeouts) live here. Mirrors
the proven read loop from ``slot_discovery`` and never raises into the caller.
"""

from __future__ import annotations

import logging
import os
import select
import termios
import time
import tty

from station_agent.slot_discovery import (
    _MAX_RESPONSE_BYTES,
    _MODULE_ID_RE,
    _extract_json,
)

logger = logging.getLogger(__name__)

_RESULT_PREFIX = "MODULE-RESULT "
_TIMEOUT_RESULT = {"ok": False, "error": "timeout"}


class SlotControl:
    def __init__(self, control_path: str, timeout: float = 3.0):
        self._path = control_path
        self._timeout = timeout

    def execute(self, module_id: str, op: str, cap: str, token: str | None = None) -> dict:
        """Send one command, return the parsed MODULE-RESULT (or a timeout error)."""
        # Defense-in-depth: never echo an unsafe token into the shell line.
        if not _MODULE_ID_RE.match(module_id) or not _MODULE_ID_RE.match(cap):
            return {"ok": False, "error": "bad_value"}
        parts = ["module", module_id, op, cap]
        if token is not None:
            parts.append(token)
        cmd = (" ".join(parts) + "\r\n").encode()

        try:
            fd = os.open(self._path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            logger.debug("slot control: cannot open %s: %s", self._path, exc)
            return dict(_TIMEOUT_RESULT)

        saved = None
        try:
            try:
                saved = termios.tcgetattr(fd)
                tty.setraw(fd)
            except termios.error:
                pass
            return self._converse(fd, cmd)
        finally:
            if saved is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, saved)
                except (termios.error, OSError):
                    pass
            try:
                os.close(fd)
            except OSError:
                pass

    def _converse(self, fd: int, cmd: bytes) -> dict:
        try:
            os.write(fd, cmd)
        except OSError:
            return dict(_TIMEOUT_RESULT)
        deadline = time.monotonic() + self._timeout
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(_TIMEOUT_RESULT)
            try:
                readable, _, _ = select.select([fd], [], [], remaining)
            except InterruptedError:
                continue
            except OSError:
                return dict(_TIMEOUT_RESULT)
            if not readable:
                continue
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return dict(_TIMEOUT_RESULT)
            if not chunk:
                return dict(_TIMEOUT_RESULT)
            buf += chunk
            if len(buf) > _MAX_RESPONSE_BYTES:
                return dict(_TIMEOUT_RESULT)
            parsed = _extract_json(buf, _RESULT_PREFIX)
            if parsed is not None:
                return parsed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_slot_control.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add station_agent/slot_control.py tests/test_slot_control.py
git commit -m "feat(agent): slot-control serial/shell transport (device driver layer)"
```

---

## Task 5: Broker command pipeline (validate → translate → execute → result + state)

**Files:**
- Create: `station_agent/broker.py`
- Test: `tests/test_broker.py`

**Interfaces:**
- Consumes: `protocol`, `descriptor`, `slot_control.SlotControl`.
- Produces:
  - `class Broker`:
    - `__init__(self, send, *, transport_factory=SlotControl, dead_man_timeout=1.5, telemetry_default_interval_ms=1000, telemetry_min_floor_ms=200, now=time.monotonic)` — `send` is an **async** callable `await send(msg: dict)`; `transport_factory(control_path) -> SlotControl`.
    - `set_inventory(discovered: list) -> None` — cache from `discover_slots` output (`[{slot, control, modules:[{id, identity, capabilities}]}]`); build `(slot, module)`→descriptor + `(slot)`→control_path indexes.
    - `async def handle(self, msg: dict) -> None` — dispatch by `msg["type"]`: `command` (this task), plus `subscribe`/`unsubscribe`/`ptt_keepalive` (Tasks 7–8, added incrementally).
    - `async def handle_command(self, msg: dict) -> None` — resolve slot/module/descriptor; `validate_command`; `format_value`; run `transport.execute` in an executor; `await send(build_result(...))`; on success also `await send(build_state(slot, module, {cap: fw_value}, ts))`.
- Produces for later tasks: `self._descriptor(slot, module) -> dict | None`, `self._control_path(slot) -> str | None`, `self._ts() -> float`.

Implementer notes:
- Resolution errors map to codes: missing slot → `unknown_slot`; missing module → `unknown_module`; both emitted as a `result` with `ok=False`.
- `get` commands: on success, `value` is the FW-returned value; still emit a `state` with `{cap: value}`.
- Blocking `transport.execute` MUST go through `await loop.run_in_executor(None, ...)` so the event loop is never blocked.
- `ts` uses the injected `now` (monotonic default) so tests can assert deterministically.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_broker.py
import asyncio
from station_agent.broker import Broker
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

FM = {
    "schema": 1, "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float",
         "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}]},
        {"name": "ptt", "kind": "action", "type": "bool"},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 250},
    ],
}


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def _broker_with_fw(fw):
    col = Collector()
    b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0), now=lambda: 100.0)
    b.set_inventory([{"slot": 1, "control": fw.control_path,
                      "modules": [{"id": "fm", "identity": FM["identity"],
                                   "capabilities": FM["capabilities"]}]}])
    return b, col


def _run(coro):
    return asyncio.run(coro)


def test_command_set_valid_flows_to_fw_and_reports_result_and_state():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(b.handle({"v": 1, "type": "command", "request_id": "r1",
                       "slot": 1, "module": "fm", "capability": "frequency",
                       "op": "set", "value": 145.5}))
        results = [m for m in col.sent if m["type"] == "result"]
        states = [m for m in col.sent if m["type"] == "state"]
        assert results[0]["request_id"] == "r1" and results[0]["ok"] is True
        assert states and states[0]["values"]["frequency"] is not None
        assert fw.state["fm"]["frequency"] == "145.5"
    finally:
        fw.stop()


def test_command_out_of_range_rejected_before_fw():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(b.handle({"v": 1, "type": "command", "request_id": "r2",
                       "slot": 1, "module": "fm", "capability": "frequency",
                       "op": "set", "value": 200.0}))
        res = [m for m in col.sent if m["type"] == "result"][0]
        assert res["ok"] is False and res["error"]["code"] == "out_of_range"
        # Never reached the firmware.
        assert "frequency" not in fw.state["fm"]
    finally:
        fw.stop()


def test_command_wrong_op_rejected_before_fw():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(b.handle({"v": 1, "type": "command", "request_id": "r3",
                       "slot": 1, "module": "fm", "capability": "frequency",
                       "op": "do", "value": 145.5}))
        res = [m for m in col.sent if m["type"] == "result"][0]
        assert res["ok"] is False and res["error"]["code"] == "wrong_op"
    finally:
        fw.stop()


def test_command_unknown_slot():
    b = Broker(Collector(), now=lambda: 1.0)
    b.set_inventory([])
    col = b._send  # type: ignore
    coll = Collector()
    b2 = Broker(coll, now=lambda: 1.0)
    b2.set_inventory([])
    _run(b2.handle({"v": 1, "type": "command", "request_id": "r4",
                    "slot": 9, "module": "fm", "capability": "rssi", "op": "get"}))
    res = [m for m in coll.sent if m["type"] == "result"][0]
    assert res["ok"] is False and res["error"]["code"] == "unknown_slot"


def test_command_get_reports_value():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(b.handle({"v": 1, "type": "command", "request_id": "r5",
                       "slot": 1, "module": "fm", "capability": "rssi", "op": "get"}))
        res = [m for m in col.sent if m["type"] == "result"][0]
        assert res["ok"] is True and isinstance(res["value"], int)
    finally:
        fw.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_broker.py -v`
Expected: FAIL (`ModuleNotFoundError: station_agent.broker`).

- [ ] **Step 3: Write minimal implementation**

```python
# station_agent/broker.py
"""Device-agnostic broker: validate -> translate -> execute -> report.

The broker turns semantic ``(slot, module, capability, op, value)`` commands into
concrete generic firmware commands, validating each against the cached ``describe``
descriptor BEFORE the firmware. It never hardcodes a module id — everything is read
from the descriptors set via ``set_inventory``. Command pipeline lives here; telemetry
subscription and the PTT dead-man are layered on in later tasks.
"""

from __future__ import annotations

import asyncio
import logging
import time

from station_agent import descriptor as desc
from station_agent import protocol as proto
from station_agent.slot_control import SlotControl

logger = logging.getLogger(__name__)


class Broker:
    def __init__(
        self,
        send,
        *,
        transport_factory=SlotControl,
        dead_man_timeout: float = 1.5,
        telemetry_default_interval_ms: int = 1000,
        telemetry_min_floor_ms: int = 200,
        now=time.monotonic,
    ):
        self._send = send
        self._transport_factory = transport_factory
        self._dead_man_timeout = dead_man_timeout
        self._telemetry_default_interval_ms = telemetry_default_interval_ms
        self._telemetry_min_floor_ms = telemetry_min_floor_ms
        self._now = now
        # (slot, module) -> descriptor dict; slot -> control path
        self._descriptors: dict[tuple[int, str], dict] = {}
        self._controls: dict[int, str] = {}

    # --- inventory cache ---------------------------------------------------
    def set_inventory(self, discovered: list) -> None:
        self._descriptors.clear()
        self._controls.clear()
        for slot_entry in discovered:
            slot = slot_entry.get("slot")
            self._controls[slot] = slot_entry.get("control", "")
            for module in slot_entry.get("modules", []):
                mid = module.get("id")
                self._descriptors[(slot, mid)] = {
                    "identity": module.get("identity", {}),
                    "capabilities": module.get("capabilities", []),
                }

    def _descriptor(self, slot, module) -> dict | None:
        return self._descriptors.get((slot, module))

    def _control_path(self, slot) -> str | None:
        return self._controls.get(slot)

    def _ts(self) -> float:
        return self._now()

    # --- dispatch ----------------------------------------------------------
    async def handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "command":
            await self.handle_command(msg)
        else:
            logger.debug("broker: ignoring message type %r", mtype)

    async def handle_command(self, msg: dict) -> None:
        request_id = msg.get("request_id")
        slot = msg.get("slot")
        module = msg.get("module")
        capability = msg.get("capability")
        op = msg.get("op")
        value = msg.get("value")

        descriptor = self._descriptor(slot, module)
        if descriptor is None:
            code = proto.UNKNOWN_SLOT if slot not in self._controls else proto.UNKNOWN_MODULE
            await self._send(proto.build_result(request_id, False, error=(code, f"{slot}/{module}")))
            return

        caps = desc.index_capabilities(descriptor)
        cap = caps.get(capability)
        try:
            desc.validate_command(cap, op, value)
        except proto.ProtocolError as exc:
            await self._send(proto.build_result(request_id, False, error=(exc.code, exc.msg)))
            return

        token = None
        if op != "get":
            token = desc.format_value(cap["type"], value)

        result = await self._execute(slot, module, op, capability, token)
        if result.get("ok"):
            await self._send(proto.build_result(request_id, True, value=result.get("value")))
            await self._send(
                proto.build_state(slot, module, {capability: result.get("value")}, self._ts())
            )
        else:
            err_code = result.get("error", proto.TIMEOUT)
            await self._send(proto.build_result(request_id, False, error=(err_code, "")))

    async def _execute(self, slot, module, op, capability, token) -> dict:
        transport = self._transport_factory(self._control_path(slot))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, transport.execute, module, op, capability, token
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_broker.py -v`
Expected: PASS (5 passed). (Note: remove the stray `col = b._send` line if your linter flags it — it's illustrative; the real assertion uses `b2`/`coll`.)

- [ ] **Step 5: Commit**

```bash
git add station_agent/broker.py tests/test_broker.py
git commit -m "feat(agent): broker command pipeline (validate/translate/execute/report)"
```

---

## Task 6: Broker inventory snapshot emission

**Files:**
- Modify: `station_agent/broker.py`
- Test: `tests/test_broker.py` (add cases)

**Interfaces:**
- Produces: `async def emit_inventory(self) -> None` — build one `inventory` message: for each slot, `modules:[{module, identity, capabilities:[<descriptor>], state:{cap:value}}]`. `state` is a one-shot best-effort snapshot of **setting** values (`op=get` per setting capability); telemetry is excluded (subscription-driven, §6). Send via `self._send`.

Implementer notes: read settings via the same `_execute` path in the executor. A failed get is simply omitted from `state` (never blocks the snapshot). Actions have no readable value here → skip. This gives multi-viewer/reconnect consistency (§5) without a reconcile loop.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_broker.py
def test_emit_inventory_includes_descriptors_and_settings_snapshot():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        # Seed a setting so the snapshot has something to read back.
        _run(b.handle({"v": 1, "type": "command", "request_id": "r0",
                       "slot": 1, "module": "fm", "capability": "frequency",
                       "op": "set", "value": 145.5}))
        col.sent.clear()
        _run(b.emit_inventory())
        inv = [m for m in col.sent if m["type"] == "inventory"][0]
        slot = inv["slots"][0]
        assert slot["slot"] == 1
        mod = slot["modules"][0]
        assert mod["module"] == "fm"
        assert any(c["name"] == "frequency" for c in mod["capabilities"])
        # frequency (a setting) is in the snapshot; rssi (telemetry) is not.
        assert "frequency" in mod["state"]
        assert "rssi" not in mod["state"]
    finally:
        fw.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_broker.py::test_emit_inventory_includes_descriptors_and_settings_snapshot -v`
Expected: FAIL (`AttributeError: 'Broker' object has no attribute 'emit_inventory'`).

- [ ] **Step 3: Write minimal implementation**

Add to `Broker`:
```python
    async def emit_inventory(self) -> None:
        slots_out = []
        # Deterministic order: sort by slot number.
        by_slot: dict[int, list] = {}
        for (slot, module), descriptor in self._descriptors.items():
            by_slot.setdefault(slot, []).append((module, descriptor))
        for slot in sorted(by_slot):
            modules_out = []
            for module, descriptor in by_slot[slot]:
                caps = descriptor.get("capabilities", [])
                state = {}
                for cap in caps:
                    if cap.get("kind") != "setting":
                        continue
                    result = await self._execute(slot, module, "get", cap["name"], None)
                    if result.get("ok"):
                        state[cap["name"]] = result.get("value")
                modules_out.append(
                    {
                        "module": module,
                        "identity": descriptor.get("identity", {}),
                        "capabilities": caps,
                        "state": state,
                    }
                )
            slots_out.append({"slot": slot, "modules": modules_out})
        await self._send(proto.build_inventory(slots_out))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_broker.py -v`
Expected: PASS (all broker tests).

- [ ] **Step 5: Commit**

```bash
git add station_agent/broker.py tests/test_broker.py
git commit -m "feat(agent): broker inventory snapshot (descriptors + settings state)"
```

---

## Task 7: Broker telemetry subscription (clamp + no-subscriber-no-poll + unsubscribe)

**Files:**
- Modify: `station_agent/broker.py`
- Test: `tests/test_broker.py` (add cases)

**Interfaces:**
- Produces:
  - `async def handle_subscribe(self, msg: dict) -> None` — `msg` = `{slot, module, capabilities[], interval_ms}`. Start/refresh a per-(slot,module) telemetry poller. Effective interval = `max(interval_ms, min_interval, telemetry_min_floor_ms)`, where `min_interval` = `descriptor.min_interval_ms(cap, telemetry_default_interval_ms)` (per cap → use the max across subscribed caps). Only telemetry caps are pollable (validate; non-telemetry → ignored/logged, never polled).
  - `async def handle_unsubscribe(self, msg: dict) -> None` — remove caps; when a (slot,module) has no caps left, cancel its poller (→ no more polling).
  - `handle()` dispatch extended: `subscribe` → `handle_subscribe`, `unsubscribe` → `handle_unsubscribe`.
  - `async def stop(self) -> None` — cancel all pollers (used on shutdown/disconnect).
  - Test seam: `def _poll_interval_s(self, slot, module) -> float | None` returns the effective poll interval (seconds) for an active subscription, else `None`.

Implementer notes:
- A poller is an `asyncio.Task` looping: `get` each subscribed telemetry cap via `_execute`, `await send(build_state(...))` with all readings, then `await asyncio.sleep(interval_s)`. No subscribers ⇒ no task ⇒ zero `execute` calls (assert via a counting transport).
- The clamp is the key spec point: a requested `interval_ms` below `min_interval` must be raised to `min_interval` (§6).
- Guard the poll loop so a transient `execute` error doesn't kill the task (log + continue).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_broker.py
class CountingTransport:
    """Transport wrapper that counts execute() calls across the whole broker."""
    def __init__(self, path):
        self._sc = SlotControl(path, timeout=2.0)
        CountingTransport.calls = getattr(CountingTransport, "calls", 0)

    def execute(self, module, op, cap, token=None):
        CountingTransport.calls += 1
        return self._sc.execute(module, op, cap, token)


def test_subscribe_clamps_interval_to_min_interval():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0),
                   telemetry_min_floor_ms=10, telemetry_default_interval_ms=1000, now=lambda: 1.0)
        b.set_inventory([{"slot": 1, "control": fw.control_path,
                          "modules": [{"id": "fm", "identity": FM["identity"],
                                       "capabilities": FM["capabilities"]}]}])

        async def scenario():
            # rssi declares min_interval_ms=250; request 50ms must clamp up to 250ms.
            await b.handle_subscribe({"slot": 1, "module": "fm",
                                      "capabilities": ["rssi"], "interval_ms": 50})
            interval = b._poll_interval_s(1, "fm")
            await b.stop()
            return interval

        interval = _run(scenario())
        assert abs(interval - 0.250) < 1e-6
    finally:
        fw.stop()


def test_no_subscriber_means_no_polling():
    CountingTransport.calls = 0
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(col, transport_factory=CountingTransport, now=lambda: 1.0)
        b.set_inventory([{"slot": 1, "control": fw.control_path,
                          "modules": [{"id": "fm", "identity": FM["identity"],
                                       "capabilities": FM["capabilities"]}]}])

        async def scenario():
            await asyncio.sleep(0.2)  # idle: nobody subscribed
            await b.stop()

        _run(scenario())
        assert CountingTransport.calls == 0
    finally:
        fw.stop()


def test_subscribe_streams_state_then_unsubscribe_stops():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0),
                   telemetry_min_floor_ms=10, telemetry_default_interval_ms=20, now=lambda: 1.0)
        b.set_inventory([{"slot": 1, "control": fw.control_path,
                          "modules": [{"id": "fm", "identity": FM["identity"],
                                       "capabilities": FM["capabilities"]}]}])

        async def scenario():
            await b.handle_subscribe({"slot": 1, "module": "fm",
                                      "capabilities": ["rssi"], "interval_ms": 20})
            await asyncio.sleep(0.12)  # a few ticks
            await b.handle_unsubscribe({"slot": 1, "module": "fm", "capabilities": ["rssi"]})
            count_after_unsub = len([m for m in col.sent if m["type"] == "state"])
            await asyncio.sleep(0.12)  # no more ticks should arrive
            final = len([m for m in col.sent if m["type"] == "state"])
            await b.stop()
            return count_after_unsub, final

        streamed, final = _run(scenario())
        assert streamed >= 1          # telemetry did stream
        assert final == streamed      # unsubscribe stopped the stream
    finally:
        fw.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_broker.py -k "subscribe or polling" -v`
Expected: FAIL (`AttributeError: handle_subscribe` / `_poll_interval_s`).

- [ ] **Step 3: Write minimal implementation**

Add to `Broker.__init__`: `self._subscriptions: dict[tuple[int, str], dict] = {}` (value = `{"caps": set(), "interval_s": float, "task": asyncio.Task}`).

Extend `handle()`:
```python
        elif mtype == "subscribe":
            await self.handle_subscribe(msg)
        elif mtype == "unsubscribe":
            await self.handle_unsubscribe(msg)
```

Add methods:
```python
    def _poll_interval_s(self, slot, module):
        sub = self._subscriptions.get((slot, module))
        return sub["interval_s"] if sub else None

    def _telemetry_caps(self, slot, module, requested):
        descriptor = self._descriptor(slot, module)
        if descriptor is None:
            return {}, self._telemetry_min_floor_ms / 1000.0
        caps = desc.index_capabilities(descriptor)
        valid, min_interval_ms = {}, self._telemetry_min_floor_ms
        for name in requested:
            cap = caps.get(name)
            if cap is None or cap.get("kind") != "telemetry":
                logger.debug("broker: ignoring non-telemetry subscribe cap %r", name)
                continue
            valid[name] = cap
            min_interval_ms = max(
                min_interval_ms, desc.min_interval_ms(cap, self._telemetry_default_interval_ms)
            )
        return valid, min_interval_ms / 1000.0

    async def handle_subscribe(self, msg: dict) -> None:
        slot, module = msg.get("slot"), msg.get("module")
        requested = msg.get("capabilities", []) or []
        interval_s = max((msg.get("interval_ms", 0) or 0) / 1000.0, 0.0)

        valid, min_interval_s = self._telemetry_caps(slot, module, requested)
        if not valid:
            return  # nothing pollable; no subscriber => no poll
        effective = max(interval_s, min_interval_s)

        key = (slot, module)
        existing = self._subscriptions.get(key)
        caps = set(valid) | (existing["caps"] if existing else set())
        if existing and existing["task"] is not None:
            existing["task"].cancel()
        task = asyncio.ensure_future(self._poll_loop(slot, module, effective))
        self._subscriptions[key] = {"caps": caps, "interval_s": effective, "task": task}

    async def handle_unsubscribe(self, msg: dict) -> None:
        slot, module = msg.get("slot"), msg.get("module")
        key = (slot, module)
        sub = self._subscriptions.get(key)
        if not sub:
            return
        sub["caps"] -= set(msg.get("capabilities", []) or [])
        if sub["task"] is not None:
            sub["task"].cancel()
        if sub["caps"]:
            sub["task"] = asyncio.ensure_future(
                self._poll_loop(slot, module, sub["interval_s"])
            )
        else:
            del self._subscriptions[key]

    async def _poll_loop(self, slot, module, interval_s: float) -> None:
        try:
            while True:
                sub = self._subscriptions.get((slot, module))
                if not sub or not sub["caps"]:
                    return
                values = {}
                for cap_name in sorted(sub["caps"]):
                    result = await self._execute(slot, module, "get", cap_name, None)
                    if result.get("ok"):
                        values[cap_name] = result.get("value")
                if values:
                    await self._send(proto.build_state(slot, module, values, self._ts()))
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a poll error must not kill the loop silently
            logger.exception("broker: telemetry poll failed for slot %s module %s", slot, module)

    async def stop(self) -> None:
        for sub in list(self._subscriptions.values()):
            if sub["task"] is not None:
                sub["task"].cancel()
        self._subscriptions.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_broker.py -v`
Expected: PASS (all broker tests, including subscription).

- [ ] **Step 5: Commit**

```bash
git add station_agent/broker.py tests/test_broker.py
git commit -m "feat(agent): broker telemetry subscription with min_interval clamp"
```

---

## Task 8: Broker PTT dead-man (keepalive timeout + disconnect → auto-unkey + event)

**Files:**
- Modify: `station_agent/broker.py`
- Test: `tests/test_broker.py` (add cases)

**Interfaces:**
- Produces:
  - PTT dead-man armed inside `handle_command` when a `do` on a `bool` **action** capability named by convention `ptt` succeeds with value `True`: start a per-(slot,module) timer task that fires after `dead_man_timeout`; on fire → local unkey (`do ptt false`) + `await send(build_event(slot, module, "ptt_auto_unkey", {"reason": "keepalive_timeout"}))`. A successful `do ptt false` disarms the timer.
  - `async def handle_keepalive(self, msg: dict) -> None` — `{slot, module}`; reset the dead-man timer. `handle()` dispatch extended: `ptt_keepalive` → `handle_keepalive`.
  - `async def on_disconnect(self) -> None` — for every armed PTT, unkey locally + emit `ptt_auto_unkey` with `{"reason": "ws_disconnect"}`, and cancel pollers (calls `stop()`).

Design notes:
- "Which capability is PTT" must stay generic: detect it as a capability whose `kind == "action"` and `type == "bool"` — the broker does not special-case the string `"fm"`; the capability name `ptt` is part of the platform vocabulary, not a module id, and is discovered from the descriptor. (Keeps the generality gate green: a second module with its own bool action + `ptt` cap gets the same dead-man for free.)
- Timer uses `asyncio.sleep(dead_man_timeout)`; keepalive cancels+restarts it.
- Unkey path reuses `_execute(slot, module, "do", ptt_cap, "false")`. Emit the event regardless of unkey result (fail-safe: we tried).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_broker.py
def test_ptt_keepalive_timeout_auto_unkeys_and_emits_event():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0),
                   dead_man_timeout=0.1, now=lambda: 1.0)
        b.set_inventory([{"slot": 1, "control": fw.control_path,
                          "modules": [{"id": "fm", "identity": FM["identity"],
                                       "capabilities": FM["capabilities"]}]}])

        async def scenario():
            await b.handle({"v": 1, "type": "command", "request_id": "k1",
                            "slot": 1, "module": "fm", "capability": "ptt",
                            "op": "do", "value": True})
            await asyncio.sleep(0.3)  # miss the keepalive window
            await b.stop()

        _run(scenario())
        events = [m for m in col.sent if m["type"] == "event" and m["event"] == "ptt_auto_unkey"]
        assert events and events[0]["detail"]["reason"] == "keepalive_timeout"
        assert fw.state["fm"]["ptt"] == "false"  # broker drove the unkey
    finally:
        fw.stop()


def test_ptt_keepalive_keeps_tx_alive():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0),
                   dead_man_timeout=0.15, now=lambda: 1.0)
        b.set_inventory([{"slot": 1, "control": fw.control_path,
                          "modules": [{"id": "fm", "identity": FM["identity"],
                                       "capabilities": FM["capabilities"]}]}])

        async def scenario():
            await b.handle({"v": 1, "type": "command", "request_id": "k2",
                            "slot": 1, "module": "fm", "capability": "ptt",
                            "op": "do", "value": True})
            for _ in range(4):
                await asyncio.sleep(0.08)
                await b.handle({"v": 1, "type": "ptt_keepalive", "slot": 1, "module": "fm"})
            early_events = [m for m in col.sent if m.get("event") == "ptt_auto_unkey"]
            await b.stop()
            return early_events

        early = _run(scenario())
        assert early == []  # kept alive; no auto-unkey while fed
    finally:
        fw.stop()


def test_ws_disconnect_unkeys_active_ptt():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0),
                   dead_man_timeout=5.0, now=lambda: 1.0)
        b.set_inventory([{"slot": 1, "control": fw.control_path,
                          "modules": [{"id": "fm", "identity": FM["identity"],
                                       "capabilities": FM["capabilities"]}]}])

        async def scenario():
            await b.handle({"v": 1, "type": "command", "request_id": "k3",
                            "slot": 1, "module": "fm", "capability": "ptt",
                            "op": "do", "value": True})
            await b.on_disconnect()  # WS dropped while keyed

        _run(scenario())
        events = [m for m in col.sent if m.get("event") == "ptt_auto_unkey"]
        assert events and events[0]["detail"]["reason"] == "ws_disconnect"
        assert fw.state["fm"]["ptt"] == "false"
    finally:
        fw.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_broker.py -k ptt -v`
Expected: FAIL (`handle_keepalive`/`on_disconnect`/dead-man not implemented).

- [ ] **Step 3: Write minimal implementation**

Add to `Broker.__init__`: `self._ptt: dict[tuple[int, str], dict] = {}` (value = `{"cap": name, "task": asyncio.Task}`).

In `handle()` dispatch add:
```python
        elif mtype == "ptt_keepalive":
            await self.handle_keepalive(msg)
```

In `handle_command`, after a successful `do` result, arm/disarm the dead-man:
```python
        if result.get("ok"):
            await self._send(proto.build_result(request_id, True, value=result.get("value")))
            await self._send(
                proto.build_state(slot, module, {capability: result.get("value")}, self._ts())
            )
            if op == "do" and self._is_ptt_cap(cap):
                if value is True:
                    self._arm_dead_man(slot, module, capability)
                elif value is False:
                    self._disarm_dead_man(slot, module)
```

Add methods:
```python
    @staticmethod
    def _is_ptt_cap(cap: dict) -> bool:
        # Generic: a bool action named 'ptt' in the platform vocabulary — no module id.
        return cap.get("kind") == "action" and cap.get("type") == "bool" and cap.get("name") == "ptt"

    def _arm_dead_man(self, slot, module, capability) -> None:
        self._disarm_dead_man(slot, module)
        task = asyncio.ensure_future(self._dead_man(slot, module, capability))
        self._ptt[(slot, module)] = {"cap": capability, "task": task}

    def _disarm_dead_man(self, slot, module) -> None:
        entry = self._ptt.pop((slot, module), None)
        if entry and entry["task"] is not None:
            entry["task"].cancel()

    async def _dead_man(self, slot, module, capability) -> None:
        try:
            await asyncio.sleep(self._dead_man_timeout)
        except asyncio.CancelledError:
            raise
        self._ptt.pop((slot, module), None)
        await self._unkey(slot, module, capability, "keepalive_timeout")

    async def handle_keepalive(self, msg: dict) -> None:
        slot, module = msg.get("slot"), msg.get("module")
        entry = self._ptt.get((slot, module))
        if not entry:
            return  # nothing keyed — keepalive is a no-op
        self._arm_dead_man(slot, module, entry["cap"])

    async def _unkey(self, slot, module, capability, reason) -> None:
        # Fail-safe: try to drive the module low, then always announce it.
        await self._execute(slot, module, "do", capability, "false")
        await self._send(proto.build_event(slot, module, "ptt_auto_unkey", {"reason": reason}))

    async def on_disconnect(self) -> None:
        for (slot, module), entry in list(self._ptt.items()):
            if entry["task"] is not None:
                entry["task"].cancel()
            del self._ptt[(slot, module)]
            await self._unkey(slot, module, entry["cap"], "ws_disconnect")
        await self.stop()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_broker.py -v`
Expected: PASS (all broker tests, including dead-man).

- [ ] **Step 5: Commit**

```bash
git add station_agent/broker.py tests/test_broker.py
git commit -m "feat(agent): PTT dead-man — auto-unkey on keepalive timeout and disconnect"
```

---

## Task 9: ControlClient — persistent Ed25519 Control-WS

**Files:**
- Create: `station_agent/control_client.py`
- Test: `tests/test_control_client.py`

**Interfaces:**
- Consumes: `station_agent.config.AgentConfig`, `station_agent.signing.load_private_key`, `station_agent.broker.Broker`, `station_agent.slot_discovery.discover_slots`, `station_agent.protocol.parse_message`.
- Produces:
  - `class ControlClient` (mirror `TerminalClient`):
    - `__init__(config)` — load Ed25519 key (raise if missing, like terminal).
    - `_build_ws_url()` — path `/ws/agent/control/{station_id}/`, same signed timestamp+body-hash query params as terminal.
    - `run()` — blocking; own asyncio loop; reconnect with the same backoff constants as terminal.
    - `stop()` — set shutdown event, cancel loop work.
    - `_connect_and_serve()` — connect; build a `Broker(send=self._ws_send, transport_factory=SlotControl, ...config knobs...)`; run discovery in an executor → `broker.set_inventory` → `await broker.emit_inventory()`; then `async for message in ws: await broker.handle(parse_message(message))`; on exit (disconnect/close) → `await broker.on_disconnect()`.
    - `async def _ws_send(self, msg: dict)` — `await self._ws.send(json.dumps(msg))`.

Design notes:
- Reuse the terminal reconnect/backoff loop verbatim (constants `BACKOFF_INITIAL/MAX/FACTOR`).
- Tests run a real in-process `websockets.serve` mock server that (a) accepts the connection, (b) captures the first inventory frame, (c) pushes a `command`, and asserts the `result`+`state` come back; plus a disconnect test that closes the socket and asserts the client reconnects.
- Keep discovery failures non-fatal (empty inventory is valid — an online station with no modules).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_client.py
import asyncio
import json
import pytest

pytest.importorskip("websockets")
import websockets

from station_agent.control_client import ControlClient
from tests.fake_fw import FakeFirmware, make_slot_tree

FM = {
    "schema": 1, "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float",
         "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}]},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 250},
    ],
}


class _FakeConfig:
    def __init__(self, server_url, station_id, key_path, slot_base):
        self.server_url = server_url
        self.station_id = station_id
        self.ed25519_key_path = key_path
        self.slot_dev_base = slot_base
        self.slot_discovery_enabled = True
        self.control_dead_man_timeout = 1.5
        self.telemetry_default_interval_ms = 1000
        self.telemetry_min_floor_ms = 200


def _gen_key(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    key = Ed25519PrivateKey.generate()
    p = tmp_path / "agent.key"
    p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    return str(p)


def test_control_client_connects_sends_inventory_and_handles_command(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = make_slot_tree(tmp_path, {1: fw})
    key_path = _gen_key(tmp_path)

    received = {"inventory": None, "result": None, "state": None}
    done = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "inventory" and received["inventory"] is None:
                received["inventory"] = msg
                await ws.send(json.dumps({"v": 1, "type": "command", "request_id": "c1",
                                          "slot": 1, "module": "fm",
                                          "capability": "frequency", "op": "set", "value": 145.5}))
            elif msg["type"] == "result":
                received["result"] = msg
            elif msg["type"] == "state":
                received["state"] = msg
                done.set()

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            client = ControlClient(cfg)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(done.wait(), timeout=8.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()

    assert received["inventory"]["slots"][0]["slot"] == 1
    assert received["result"]["ok"] is True and received["result"]["request_id"] == "c1"
    assert "frequency" in received["state"]["values"]
    assert fw.state["fm"]["frequency"] == "145.5"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_control_client.py -v`
Expected: FAIL (`ModuleNotFoundError: station_agent.control_client`).

- [ ] **Step 3: Write minimal implementation**

```python
# station_agent/control_client.py
"""Persistent outbound Control-WebSocket for the Station Agent.

Reuses the proven Ed25519 outbound-WS pattern from terminal.py (signed timestamp +
body-hash query params, exponential reconnect backoff), but is PERSISTENT while the
station is online (design spec §9). It runs the device-agnostic Broker: on connect it
discovers modules, pushes an ``inventory`` snapshot, then relays server ``command`` /
``subscribe`` / ``unsubscribe`` / ``ptt_keepalive`` into the broker and the broker's
``inventory`` / ``state`` / ``result`` / ``event`` back up. A disconnect fires the PTT
dead-man locally.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import threading
import time
from urllib.parse import urlencode

import websockets

from .broker import Broker
from .config import AgentConfig
from .protocol import ProtocolError, parse_message
from .signing import load_private_key
from .slot_control import SlotControl
from .slot_discovery import discover_slots

logger = logging.getLogger(__name__)

BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 60.0
BACKOFF_FACTOR = 2.0


class ControlClient:
    def __init__(self, config: AgentConfig):
        self._config = config
        self._ws = None
        self._shutdown = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._private_key = load_private_key(config.ed25519_key_path)
        if self._private_key is None:
            raise RuntimeError(
                "Control: Ed25519 key could not be loaded; WebSocket authentication is impossible"
            )

    def _build_ws_url(self) -> str:
        server = self._config.server_url
        if server.startswith("https://"):
            ws_base = "wss://" + server[len("https://"):]
        elif server.startswith("http://"):
            ws_base = "ws://" + server[len("http://"):]
        else:
            ws_base = "wss://" + server
        path = f"/ws/agent/control/{self._config.station_id}/"
        timestamp = str(time.time())
        body_hash = hashlib.sha256(b"").hexdigest()
        signature = self._private_key.sign(f"{timestamp}:{body_hash}".encode())
        query = {
            "station_id": str(self._config.station_id),
            "signature": base64.b64encode(signature).decode("ascii"),
            "timestamp": timestamp,
        }
        return f"{ws_base}{path}?{urlencode(query)}"

    async def _ws_send(self, msg: dict) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(msg))

    async def _connect_and_serve(self) -> None:
        url = self._build_ws_url()
        logger.info("Control: connecting to server")
        async with websockets.connect(
            url, ping_interval=30, ping_timeout=10, close_timeout=5
        ) as ws:
            self._ws = ws
            broker = Broker(
                self._ws_send,
                transport_factory=SlotControl,
                dead_man_timeout=getattr(self._config, "control_dead_man_timeout", 1.5),
                telemetry_default_interval_ms=getattr(
                    self._config, "telemetry_default_interval_ms", 1000
                ),
                telemetry_min_floor_ms=getattr(self._config, "telemetry_min_floor_ms", 200),
            )
            loop = asyncio.get_running_loop()
            try:
                discovered = await loop.run_in_executor(
                    None, discover_slots, self._config.slot_dev_base
                )
            except Exception:  # noqa: BLE001 — discovery must not break the control link
                logger.exception("Control: slot discovery failed; reporting empty inventory")
                discovered = []
            broker.set_inventory(discovered)
            await broker.emit_inventory()
            logger.info("Control: connected, inventory sent")

            try:
                async for message in ws:
                    if self._shutdown.is_set():
                        break
                    try:
                        parsed = parse_message(message)
                    except ProtocolError as exc:
                        logger.warning("Control: dropping malformed message: %s", exc)
                        continue
                    await broker.handle(parsed)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.info("Control: WebSocket closed (code=%s)", exc.code)
            finally:
                await broker.on_disconnect()
                self._ws = None

    async def _run_async(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self._shutdown.is_set():
            try:
                await self._connect_and_serve()
                backoff = BACKOFF_INITIAL
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                logger.warning("Control: connection error (%s), retrying in %.0fs", exc, backoff)
            except Exception as exc:  # noqa: BLE001
                logger.error("Control: unexpected error (%s: %s), retrying in %.0fs",
                             type(exc).__name__, exc, backoff)
            if self._shutdown.is_set():
                break
            wait_end = time.monotonic() + backoff
            while time.monotonic() < wait_end and not self._shutdown.is_set():
                await asyncio.sleep(0.5)
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
        logger.info("Control: client stopped")

    def run(self) -> None:
        logger.info("Control: starting client")
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._run_async())
        except Exception as exc:  # noqa: BLE001
            logger.error("Control: event loop error: %s", exc)
        finally:
            self._loop.close()
            self._loop = None

    def stop(self) -> None:
        logger.info("Control: stop requested")
        self._shutdown.set()
        if self._ws is not None and self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_control_client.py -v`
Expected: PASS (1 passed). If flaky on timing, raise the `done.wait` timeout — do not weaken assertions.

- [ ] **Step 5: Commit**

```bash
git add station_agent/control_client.py tests/test_control_client.py
git commit -m "feat(agent): persistent Ed25519 Control-WS client running the broker"
```

---

## Task 10: Wire ControlClient into the agent + config

**Files:**
- Modify: `station_agent/config.py`
- Modify: `station_agent/agent.py`
- Modify: `station_agent/config.example.yml`
- Test: `tests/test_config.py` (add cases; create the file if absent)

**Interfaces:**
- Produces on `AgentConfig`: `control_enabled: bool = False`, `control_dead_man_timeout: float = 1.5`, `telemetry_default_interval_ms: int = 1000`, `telemetry_min_floor_ms: int = 200`, loaded from YAML in `load_config`.
- `agent.py`: when `config.control_enabled`, start `ControlClient(config).run()` in a daemon thread named `control-client` (mirror the terminal wiring), and `stop()`+`join(timeout=5)` on shutdown.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (add; create if missing)
import os
from station_agent.config import load_config


def _write(tmp_path, body):
    p = tmp_path / "config.yml"
    p.write_text(body)
    os.environ["STATION_AGENT_CONFIG"] = str(p)
    return p


def test_control_defaults_off(tmp_path):
    _write(tmp_path, "server_url: http://x\nstation_id: 1\ned25519_key_path: /k\n")
    cfg = load_config()
    assert cfg.control_enabled is False
    assert cfg.control_dead_man_timeout == 1.5
    assert cfg.telemetry_default_interval_ms == 1000
    assert cfg.telemetry_min_floor_ms == 200


def test_control_enabled_from_yaml(tmp_path):
    _write(tmp_path, (
        "server_url: http://x\nstation_id: 1\ned25519_key_path: /k\n"
        "control_enabled: true\ncontrol_dead_man_timeout: 2.0\n"
        "telemetry_default_interval_ms: 500\ntelemetry_min_floor_ms: 100\n"
    ))
    cfg = load_config()
    assert cfg.control_enabled is True
    assert cfg.control_dead_man_timeout == 2.0
    assert cfg.telemetry_default_interval_ms == 500
    assert cfg.telemetry_min_floor_ms == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd station-manager && python -m pytest tests/test_config.py -v`
Expected: FAIL (`AttributeError: control_enabled`).

- [ ] **Step 3: Write minimal implementation**

In `config.py` `AgentConfig` add fields:
```python
    control_enabled: bool = False
    control_dead_man_timeout: float = 1.5
    telemetry_default_interval_ms: int = 1000
    telemetry_min_floor_ms: int = 200
```
In `load_config()` add to the `AgentConfig(...)` construction:
```python
        control_enabled=bool(data.get("control_enabled", False)),
        control_dead_man_timeout=float(data.get("control_dead_man_timeout", 1.5)),
        telemetry_default_interval_ms=int(data.get("telemetry_default_interval_ms", 1000)),
        telemetry_min_floor_ms=int(data.get("telemetry_min_floor_ms", 200)),
```

In `agent.py`, after the terminal-thread block, add (mirror pattern):
```python
        from .control_client import ControlClient  # local import: keeps agent import light

        control_client = None
        control_thread = None
        if config.control_enabled:
            logger.info("Control channel enabled")
            control_client = ControlClient(config)
            control_thread = threading.Thread(
                target=control_client.run, name="control-client", daemon=True
            )
            control_thread.start()
        else:
            logger.info("Control channel disabled")
```
And in the shutdown section, after stopping the terminal client:
```python
        if control_client is not None:
            control_client.stop()
        if control_thread is not None:
            control_thread.join(timeout=5)
```

In `config.example.yml`, document the new keys:
```yaml
# Persistent control channel (D3 agent broker). Off by default.
control_enabled: false
# PTT dead-man: auto-unkey if no keepalive within this many seconds.
control_dead_man_timeout: 1.5
# Telemetry subscription: default poll interval and the hard floor (ms).
telemetry_default_interval_ms: 1000
telemetry_min_floor_ms: 200
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd station-manager && python -m pytest tests/test_config.py -v`
Expected: PASS. Then smoke-import the agent: `python -c "import station_agent.agent"` → no error.

- [ ] **Step 5: Commit**

```bash
git add station_agent/config.py station_agent/agent.py station_agent/config.example.yml tests/test_config.py
git commit -m "feat(agent): wire ControlClient into the agent loop + config knobs"
```

---

## Task 11: Generality gate — a second fictitious module flows through unchanged

**Files:**
- Create: `tests/test_broker_generic.py`

**Interfaces:** none new. This is the DoD gate proving no module id is hardcoded: a second, invented module (`beacon`, with a bool `ptt`-analogue action, an int setting, and an int telemetry) is discovered, commanded, subscribed, and dead-manned through the **same** broker code with zero changes.

- [ ] **Step 1: Write the failing test (fails only if the broker is not generic)**

```python
# tests/test_broker_generic.py
import asyncio
from station_agent.broker import Broker
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

# A totally different, invented module — NOT "fm". Same generic contract.
BEACON = {
    "schema": 1, "module": "beacon",
    "identity": {"type": "beacon_tx", "model": "BCN-1", "version": "1"},
    "capabilities": [
        {"name": "interval", "kind": "setting", "type": "int", "ranges": [{"min": 1, "max": 60}]},
        {"name": "ptt", "kind": "action", "type": "bool"},
        {"name": "temperature", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 100},
    ],
}


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def test_second_module_flows_through_broker_unchanged(tmp_path):
    fw = FakeFirmware({"beacon": BEACON})
    fw.start()
    try:
        col = Collector()
        b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0),
                   dead_man_timeout=0.1, telemetry_min_floor_ms=10,
                   telemetry_default_interval_ms=20, now=lambda: 1.0)
        b.set_inventory([{"slot": 2, "control": fw.control_path,
                          "modules": [{"id": "beacon", "identity": BEACON["identity"],
                                       "capabilities": BEACON["capabilities"]}]}])

        async def scenario():
            # 1) inventory
            await b.emit_inventory()
            # 2) valid set + 3) range reject, both without any 'fm'/'beacon' special-casing
            await b.handle({"v": 1, "type": "command", "request_id": "g1", "slot": 2,
                            "module": "beacon", "capability": "interval", "op": "set", "value": 10})
            await b.handle({"v": 1, "type": "command", "request_id": "g2", "slot": 2,
                            "module": "beacon", "capability": "interval", "op": "set", "value": 99})
            # 4) telemetry subscription streams, then unsubscribe
            await b.handle_subscribe({"slot": 2, "module": "beacon",
                                      "capabilities": ["temperature"], "interval_ms": 20})
            await asyncio.sleep(0.1)
            await b.handle_unsubscribe({"slot": 2, "module": "beacon",
                                        "capabilities": ["temperature"]})
            # 5) PTT dead-man on the invented module's bool action
            await b.handle({"v": 1, "type": "command", "request_id": "g3", "slot": 2,
                            "module": "beacon", "capability": "ptt", "op": "do", "value": True})
            await asyncio.sleep(0.3)
            await b.stop()

        _run = asyncio.run
        _run(scenario())

        results = [m for m in col.sent if m["type"] == "result"]
        assert any(r["request_id"] == "g1" and r["ok"] for r in results)
        assert any(r["request_id"] == "g2" and not r["ok"]
                   and r["error"]["code"] == "out_of_range" for r in results)
        assert [m for m in col.sent if m["type"] == "state"]           # telemetry streamed
        assert any(m.get("event") == "ptt_auto_unkey" for m in col.sent)  # dead-man fired
        assert fw.state["beacon"]["ptt"] == "false"
    finally:
        fw.stop()
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `cd station-manager && python -m pytest tests/test_broker_generic.py -v`
Expected: PASS if the broker is truly generic. If it FAILS, the broker has a hidden module-id assumption — fix the broker, not the test.

- [ ] **Step 3: Prove no hardcoded module id in the broker**

Run: `cd station-manager && grep -n '"fm"\|'"'"'fm'"'"'\|beacon' station_agent/broker.py`
Expected: no matches (empty output). If any match, remove the special-casing.

- [ ] **Step 4: Full suite green**

Run: `cd station-manager && python -m pytest tests/test_protocol.py tests/test_descriptor.py tests/test_fake_fw.py tests/test_slot_control.py tests/test_broker.py tests/test_control_client.py tests/test_config.py tests/test_broker_generic.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_broker_generic.py
git commit -m "test(agent): generality gate — invented module flows through broker unchanged"
```

---

## Task 12: Verification & regression sweep

**Files:** none (verification only).

- [ ] **Step 1: Run the full station_agent + existing D2 suite**

Run: `cd station-manager && python -m pytest tests/ -k "protocol or descriptor or fake_fw or slot or broker or control or config or terminal or inventory" -v`
Expected: all PASS, including the pre-existing `test_slot_discovery.py`, `test_heartbeat_inventory.py`, `test_terminal_agent.py` (no regressions from the `fake_fw` extraction or `slot_control` reuse of `slot_discovery` internals).

- [ ] **Step 2: Lint**

Run: `cd station-manager && ruff check station_agent/ tests/` (if `ruff` is configured; the repo has a `.ruff_cache`).
Expected: clean, or fix reported issues.

- [ ] **Step 3: Import smoke test**

Run: `cd station-manager && python -c "import station_agent.agent, station_agent.control_client, station_agent.broker, station_agent.slot_control, station_agent.descriptor, station_agent.protocol; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: DoD self-check against spec §12**
  - Broker discovers modules generically, caches descriptors, emits `inventory` ✅ (Tasks 5–6, 9)
  - `command` over Control-WS → validate → translate → execute vs native_sim pty; `result` + `state` correct ✅ (Tasks 5, 9)
  - Subscription streams at clamped rate; no subscriber → no poll; unsubscribe stops ✅ (Task 7)
  - PTT dead-man auto-unkeys on keepalive timeout AND WS disconnect; `event` emitted ✅ (Task 8)
  - No `"fm"` in the broker; generality test green ✅ (Task 11)

- [ ] **Step 5: Use superpowers:verification-before-completion** before any "done" claim, then open the PR with `Closes #88` and run the copilot-loop.

---

## Self-Review (author checklist — completed)

**Spec coverage:**
- §2 principles (one vocabulary, generic `(slot,module,capability)`, descriptor-driven) → Tasks 2, 5, 11.
- §3 three layers (descriptor / setting-value / telemetry) → `inventory` (Task 6), `state` after command (Task 5), telemetry `state` (Task 7).
- §4 broker pipeline → Tasks 2, 4, 5.
- §5 hybrid push (state after every command + inventory on connect) → Tasks 5, 6, 9.
- §6 subscription + min_interval clamp + no-subscriber-no-poll → Task 7.
- §7 message set (inventory/state/result/event ↑; command/subscribe/unsubscribe/ptt_keepalive ↓) → Tasks 1, 5, 6, 7, 8.
- §8 PTT dead-man (timeout + disconnect) → Task 8; TX-lock is server-side/out-of-scope (noted).
- §9 persistent Ed25519 Control-WS, terminal pattern, disconnect→dead-man → Task 9.
- §10 versioning (`v` vs `schema`) + structured errors → Task 1, threaded through.
- §11 scope (agent-side only; server is D4) → ControlClient tested vs mock server (Task 9).
- §12 testing (validate/translate/execute; clamp; dead-man; generality) → Tasks 5, 7, 8, 11.

**Placeholder scan:** none — every step carries complete code/commands.

**Type consistency:** `send` is async everywhere; `SlotControl.execute(module, op, cap, token)` signature identical in transport, broker, and tests; error tuples `(code, msg)` consistent between `descriptor.validate_command` (raises `ProtocolError`) and `protocol.build_result`; `set_inventory` consumes the exact `discover_slots` shape.

**Companion FW note:** `min_interval_ms` is read opportunistically from the descriptor (Task 2 `min_interval_ms`); if FW-RemoteStation #52 hasn't landed, the config default applies — no hard dependency, DoD still met.
