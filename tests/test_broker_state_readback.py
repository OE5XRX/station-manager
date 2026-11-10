"""After a successful setting change the agent reports the module's *whole* settings state.

The station is the single source of truth: setting ``frequency`` makes the FW move rx+tx, and
the browser must see that, not only the capability it asked for.
"""

import asyncio

from station_agent.broker import Broker
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

_F = {"kind": "setting", "type": "float", "ranges": [{"min": 100.0, "max": 500.0}]}
FM = {
    "schema": 1,
    "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {**_F, "name": "frequency"},
        {**_F, "name": "rx_frequency"},
        {**_F, "name": "tx_frequency"},
        {"name": "tx_tone", "kind": "setting", "type": "string"},
        {"name": "ptt", "kind": "action", "type": "bool"},
        {"name": "rssi", "kind": "telemetry", "type": "int"},
    ],
}


class LinkedFirmware(FakeFirmware):
    """Like the real FW: ``frequency`` also moves rx_frequency and tx_frequency."""

    def _handle_cmd(self, mid, op, cap, token):
        super()._handle_cmd(mid, op, cap, token)
        if op == "set" and cap == "frequency":
            self.state[mid]["rx_frequency"] = token
            self.state[mid]["tx_frequency"] = token


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def _setup():
    fw = LinkedFirmware({"fm": FM})
    fw.start()
    fw.state["fm"].update({"rx_frequency": "1", "tx_frequency": "1", "tx_tone": "none"})
    col = Collector()
    b = Broker(
        col,
        transport_factory=lambda p: SlotControl(p, timeout=2.0),
        dead_man_timeout=5.0,
        now=lambda: 1.0,
    )
    b.set_inventory(
        [
            {
                "slot": 1,
                "control": fw.control_path,
                "modules": [
                    {"id": "fm", "identity": FM["identity"], "capabilities": FM["capabilities"]}
                ],
            }
        ]
    )
    return fw, b, col


def _set(cap, value):
    return {
        "v": 1,
        "type": "command",
        "request_id": "r1",
        "slot": 1,
        "module": "fm",
        "capability": cap,
        "op": "set",
        "value": value,
    }


def _states(col):
    return [m for m in col.sent if m["type"] == "state"]


def test_set_reports_all_settings_not_only_the_set_cap():
    fw, b, col = _setup()
    try:
        asyncio.run(b.handle(_set("frequency", 145.5)))
        states = _states(col)
        assert len(states) == 1
        vals = states[0]["values"]
        assert set(vals) == {"frequency", "rx_frequency", "tx_frequency", "tx_tone"}
        assert vals["rx_frequency"] == "145.5" and vals["tx_frequency"] == "145.5"
        assert vals["tx_tone"] == "none"
        assert "rssi" not in vals and "ptt" not in vals  # telemetry/actions are not settings
    finally:
        fw.stop()


def test_failing_readback_get_is_omitted_not_fatal():
    fw, b, col = _setup()
    try:
        real = b._execute

        async def flaky(slot, module, op, cap, token, **kw):
            if op == "get" and cap == "tx_tone":
                return {"ok": False, "error": "timeout"}
            return await real(slot, module, op, cap, token, **kw)

        b._execute = flaky
        asyncio.run(b.handle(_set("frequency", 145.5)))
        vals = _states(col)[0]["values"]
        assert "tx_tone" not in vals
        assert vals["rx_frequency"] == "145.5"
    finally:
        fw.stop()


def test_raising_readback_get_is_omitted_not_fatal():
    fw, b, col = _setup()
    try:
        real = b._execute

        async def boom(slot, module, op, cap, token, **kw):
            if op == "get" and cap == "tx_tone":
                raise RuntimeError("boom")
            return await real(slot, module, op, cap, token, **kw)

        b._execute = boom
        asyncio.run(b.handle(_set("frequency", 145.5)))
        assert "tx_tone" not in _states(col)[0]["values"]
    finally:
        fw.stop()


def test_set_falls_back_to_set_value_if_every_readback_fails():
    fw, b, col = _setup()
    try:
        real = b._execute

        async def nogets(slot, module, op, cap, token, **kw):
            if op == "get":
                return {"ok": False, "error": "timeout"}
            return await real(slot, module, op, cap, token, **kw)

        b._execute = nogets
        asyncio.run(b.handle(_set("frequency", 145.5)))
        vals = _states(col)[0]["values"]
        assert vals == {"frequency": "145.5"}
    finally:
        fw.stop()


def test_result_is_sent_before_readback_state():
    fw, b, col = _setup()
    try:
        asyncio.run(b.handle(_set("frequency", 145.5)))
        assert [m["type"] for m in col.sent] == ["result", "state"]
    finally:
        fw.stop()


def test_no_readback_while_slot_is_keyed():
    fw, b, col = _setup()
    try:
        b._ptt_inflight.add((1, "fm"))  # keyed / being keyed
        asyncio.run(b.handle(_set("tx_tone", "77.0")))
        gets = [ln for ln in map(bytes.decode, fw.received) if " get " in ln]
        assert gets == []  # no extra serial traffic on a keyed slot
        assert _states(col)[0]["values"] == {"tx_tone": "77.0"}
    finally:
        fw.stop()


def test_apply_preset_reports_full_snapshot():
    fw, b, col = _setup()
    try:
        asyncio.run(
            b.handle(
                {
                    "v": 1,
                    "type": "apply_preset",
                    "request_id": "p1",
                    "slot": 1,
                    "module": "fm",
                    "values": {"tx_frequency": 145.0, "rx_frequency": 999.0},
                }
            )
        )
        states = _states(col)
        assert len(states) == 1
        vals = states[0]["values"]
        # rx_frequency failed to apply, but the snapshot still reports the real (old) value.
        assert vals["tx_frequency"] == "145.0" and vals["rx_frequency"] == "1"
        assert {"frequency", "tx_tone"} <= set(vals)
    finally:
        fw.stop()


def test_apply_preset_with_no_ok_cap_sends_no_state():
    fw, b, col = _setup()
    try:
        asyncio.run(
            b.handle(
                {
                    "v": 1,
                    "type": "apply_preset",
                    "request_id": "p1",
                    "slot": 1,
                    "module": "fm",
                    "values": {"rx_frequency": 999.0},
                }
            )
        )
        assert _states(col) == []
    finally:
        fw.stop()


def _count_gets(fw):
    return len([ln for ln in map(bytes.decode, fw.received) if " get " in ln])


def test_no_readback_while_slot_paused(monkeypatch):
    from station_agent import slot_pause

    fw, b, col = _setup()
    try:
        real = b._execute
        paused = {"on": False}

        async def pausing(slot, module, op, cap, token, **kw):
            res = await real(slot, module, op, cap, token, **kw)
            if op == "set":
                paused["on"] = True  # slot gets paused right after the set succeeded
            return res

        b._execute = pausing
        monkeypatch.setattr(slot_pause, "is_paused", lambda s: paused["on"])
        asyncio.run(b.handle(_set("frequency", 145.5)))
        assert _count_gets(fw) == 0
        assert _states(col)[0]["values"] == {"frequency": "145.5"}
    finally:
        fw.stop()


def test_slot_keyed_mid_snapshot_stops_readback():
    fw, b, col = _setup()
    try:
        real = b._execute

        async def key_after_first_get(slot, module, op, cap, token, **kw):
            res = await real(slot, module, op, cap, token, **kw)
            if op == "get":
                b._ptt_inflight.add((slot, module))
            return res

        b._execute = key_after_first_get
        asyncio.run(b.handle(_set("frequency", 145.5)))
        assert _count_gets(fw) == 1
    finally:
        fw.stop()


def test_timeout_aborts_remaining_readbacks():
    fw, b, col = _setup()
    try:
        calls = []

        async def dead(slot, module, op, cap, token, **kw):
            calls.append((op, cap))
            if op == "get":
                return {"ok": False, "error": "timeout"}
            return {"ok": True, "value": token}

        b._execute = dead
        asyncio.run(b.handle(_set("frequency", 145.5)))
        assert [c for c in calls if c[0] == "get"] == [("get", "frequency")]
        assert _states(col)[0]["values"] == {"frequency": "145.5"}
    finally:
        fw.stop()


def test_readback_budget_bounds_snapshot():
    fw, b, col = _setup()
    try:
        b._READBACK_BUDGET_S = 0.0
        asyncio.run(b.handle(_set("frequency", 145.5)))
        assert _count_gets(fw) == 0
        assert _states(col)[0]["values"] == {"frequency": "145.5"}
    finally:
        fw.stop()


def test_virtual_module_set_gets_no_readback():
    fw, b, col = _setup()
    try:
        b._virtual[(1, "fm")] = object()
        values = asyncio.run(b._settings_snapshot(1, "fm", {"x": 1}))
        assert values == {"x": 1} and _count_gets(fw) == 0
    finally:
        fw.stop()
