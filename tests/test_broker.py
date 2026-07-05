# tests/test_broker.py
import asyncio

from station_agent.broker import Broker
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware


class CountingTransport:
    """Transport wrapper that counts execute() calls across the whole broker."""

    def __init__(self, path):
        self._sc = SlotControl(path, timeout=2.0)
        CountingTransport.calls = getattr(CountingTransport, "calls", 0)

    def execute(self, module, op, cap, token=None):
        CountingTransport.calls += 1
        return self._sc.execute(module, op, cap, token)


FM = {
    "schema": 1,
    "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {
            "name": "frequency",
            "kind": "setting",
            "type": "float",
            "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}],
        },
        {"name": "ptt", "kind": "action", "type": "bool"},
        {
            "name": "rssi",
            "kind": "telemetry",
            "type": "int",
            "readonly": True,
            "min_interval_ms": 250,
        },
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
    return b, col


def _run(coro):
    return asyncio.run(coro)


def test_command_set_valid_flows_to_fw_and_reports_result_and_state():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(
            b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "r1",
                    "slot": 1,
                    "module": "fm",
                    "capability": "frequency",
                    "op": "set",
                    "value": 145.5,
                }
            )
        )
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
        _run(
            b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "r2",
                    "slot": 1,
                    "module": "fm",
                    "capability": "frequency",
                    "op": "set",
                    "value": 200.0,
                }
            )
        )
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
        _run(
            b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "r3",
                    "slot": 1,
                    "module": "fm",
                    "capability": "frequency",
                    "op": "do",
                    "value": 145.5,
                }
            )
        )
        res = [m for m in col.sent if m["type"] == "result"][0]
        assert res["ok"] is False and res["error"]["code"] == "wrong_op"
    finally:
        fw.stop()


def test_command_unknown_slot():
    coll = Collector()
    b2 = Broker(coll, now=lambda: 1.0)
    b2.set_inventory([])
    _run(
        b2.handle(
            {
                "v": 1,
                "type": "command",
                "request_id": "r4",
                "slot": 9,
                "module": "fm",
                "capability": "rssi",
                "op": "get",
            }
        )
    )
    res = [m for m in coll.sent if m["type"] == "result"][0]
    assert res["ok"] is False and res["error"]["code"] == "unknown_slot"


def test_command_get_reports_value():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(
            b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "r5",
                    "slot": 1,
                    "module": "fm",
                    "capability": "rssi",
                    "op": "get",
                }
            )
        )
        res = [m for m in col.sent if m["type"] == "result"][0]
        assert res["ok"] is True and isinstance(res["value"], int)
    finally:
        fw.stop()


def test_emit_inventory_includes_descriptors_and_settings_snapshot():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        # Seed a setting so the snapshot has something to read back.
        _run(
            b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "r0",
                    "slot": 1,
                    "module": "fm",
                    "capability": "frequency",
                    "op": "set",
                    "value": 145.5,
                }
            )
        )
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


def test_subscribe_clamps_interval_to_min_interval():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(
            col,
            transport_factory=lambda p: SlotControl(p, timeout=2.0),
            telemetry_min_floor_ms=10,
            telemetry_default_interval_ms=1000,
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            # rssi declares min_interval_ms=250; request 50ms must clamp up to 250ms.
            await b.handle_subscribe(
                {"slot": 1, "module": "fm", "capabilities": ["rssi"], "interval_ms": 50}
            )
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
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            await asyncio.sleep(0.2)  # idle: nobody subscribed
            await b.stop()

        _run(scenario())
        assert CountingTransport.calls == 0
    finally:
        fw.stop()


def test_additive_resubscribe_clamps_to_slowest_cap():
    """Re-subscribe adding a slower cap must raise the effective interval (spec §6)."""
    # Local descriptor with two telemetry caps of different min_intervals.
    two_cap = {
        "identity": {"type": "test", "model": "test", "version": "v0"},
        "capabilities": [
            {
                "name": "fast",
                "kind": "telemetry",
                "type": "int",
                "readonly": True,
                "min_interval_ms": 100,
            },
            {
                "name": "slow",
                "kind": "telemetry",
                "type": "int",
                "readonly": True,
                "min_interval_ms": 500,
            },
        ],
    }

    def make_broker():
        col = Collector()
        b = Broker(
            col,
            transport_factory=lambda p: None,
            telemetry_min_floor_ms=10,
            telemetry_default_interval_ms=1000,
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": "/dev/null",
                    "modules": [
                        {
                            "id": "two",
                            "identity": two_cap["identity"],
                            "capabilities": two_cap["capabilities"],
                        }
                    ],
                }
            ]
        )
        return b

    async def scenario_fast_then_slow():
        b = make_broker()
        # Subscribe fast first with a very low requested interval.
        await b.handle_subscribe(
            {"slot": 1, "module": "two", "capabilities": ["fast"], "interval_ms": 50}
        )
        # Now add slow — merged set {fast, slow} must clamp to >=500ms.
        await b.handle_subscribe(
            {"slot": 1, "module": "two", "capabilities": ["slow"], "interval_ms": 50}
        )
        interval = b._poll_interval_s(1, "two")
        await b.stop()
        return interval

    async def scenario_slow_then_fast():
        b = make_broker()
        # Subscribe slow first.
        await b.handle_subscribe(
            {"slot": 1, "module": "two", "capabilities": ["slow"], "interval_ms": 50}
        )
        # Now add fast — merged set {fast, slow} must still clamp to >=500ms.
        await b.handle_subscribe(
            {"slot": 1, "module": "two", "capabilities": ["fast"], "interval_ms": 50}
        )
        interval = b._poll_interval_s(1, "two")
        await b.stop()
        return interval

    interval_a = _run(scenario_fast_then_slow())
    interval_b = _run(scenario_slow_then_fast())

    assert interval_a >= 0.5, f"fast-then-slow: expected >=0.5s, got {interval_a}"
    assert interval_b >= 0.5, f"slow-then-fast: expected >=0.5s, got {interval_b}"


def test_ptt_keepalive_timeout_auto_unkeys_and_emits_event():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(
            col,
            transport_factory=lambda p: SlotControl(p, timeout=2.0),
            dead_man_timeout=0.1,
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            await b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "k1",
                    "slot": 1,
                    "module": "fm",
                    "capability": "ptt",
                    "op": "do",
                    "value": True,
                }
            )
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
        b = Broker(
            col,
            transport_factory=lambda p: SlotControl(p, timeout=2.0),
            dead_man_timeout=0.15,
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            await b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "k2",
                    "slot": 1,
                    "module": "fm",
                    "capability": "ptt",
                    "op": "do",
                    "value": True,
                }
            )
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
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            await b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "k3",
                    "slot": 1,
                    "module": "fm",
                    "capability": "ptt",
                    "op": "do",
                    "value": True,
                }
            )
            await b.on_disconnect()  # WS dropped while keyed

        _run(scenario())
        events = [m for m in col.sent if m.get("event") == "ptt_auto_unkey"]
        assert events and events[0]["detail"]["reason"] == "ws_disconnect"
        assert fw.state["fm"]["ptt"] == "false"
    finally:
        fw.stop()


def test_stop_cancels_ptt_timer_without_unkeying():
    """stop() must cancel PTT dead-man timers; must NOT emit ptt_auto_unkey."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(
            col,
            transport_factory=lambda p: SlotControl(p, timeout=2.0),
            dead_man_timeout=5.0,  # long enough that it won't fire on its own
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            # Arm the PTT (ptt true).
            await b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "s1",
                    "slot": 1,
                    "module": "fm",
                    "capability": "ptt",
                    "op": "do",
                    "value": True,
                }
            )
            # Immediately stop — timer should be cancelled, NOT fired.
            await b.stop()
            await asyncio.sleep(0.05)
            events = [m for m in col.sent if m.get("event") == "ptt_auto_unkey"]
            return events

        events = _run(scenario())
        assert events == [], "stop() must not emit ptt_auto_unkey"
        # fw ptt state stays "true" — stop() did not unkey.
        assert fw.state["fm"]["ptt"] == "true"
    finally:
        fw.stop()


def test_emit_inventory_skips_malformed_cap_without_name():
    """A cap dict missing 'name' must be silently skipped; valid caps still appear."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        # Inject a malformed cap (no "name") alongside the real FM caps.
        b._descriptors[(1, "fm")]["capabilities"] = [
            {"kind": "setting", "type": "int"},  # malformed: no name
            {
                "name": "frequency",
                "kind": "setting",
                "type": "float",
                "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}],
            },
        ]
        # Seed a value so the get has something to return.
        _run(
            b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "ma0",
                    "slot": 1,
                    "module": "fm",
                    "capability": "frequency",
                    "op": "set",
                    "value": 145.5,
                }
            )
        )
        col.sent.clear()
        # Must not raise.
        _run(b.emit_inventory())
        inv_msgs = [m for m in col.sent if m["type"] == "inventory"]
        assert inv_msgs, "inventory message must be emitted"
        state = inv_msgs[0]["slots"][0]["modules"][0]["state"]
        assert "frequency" in state, "valid setting must appear in state snapshot"
        # Malformed cap has no name so it cannot appear in state.
        for key in state:
            assert key, "all state keys must be non-empty strings"
    finally:
        fw.stop()


def test_subscribe_missing_interval_ms_uses_default():
    """Omitting interval_ms must fall back to telemetry_default_interval_ms."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(
            col,
            transport_factory=lambda p: SlotControl(p, timeout=2.0),
            telemetry_default_interval_ms=800,
            telemetry_min_floor_ms=10,
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            # rssi has min_interval_ms=250, which is < 800ms default
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            # No interval_ms key — must use default (800ms), not clamp to 250ms.
            await b.handle_subscribe({"slot": 1, "module": "fm", "capabilities": ["rssi"]})
            interval = b._poll_interval_s(1, "fm")
            await b.stop()
            return interval

        interval = _run(scenario())
        assert abs(interval - 0.8) < 1e-6, f"expected 0.8s, got {interval}"
    finally:
        fw.stop()


def test_subscribe_with_malformed_capabilities_does_not_raise_or_subscribe():
    """handle_subscribe with non-list capabilities must not raise and must not subscribe."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)

        async def scenario():
            # int capabilities
            await b.handle_subscribe(
                {"slot": 1, "module": "fm", "capabilities": 123, "interval_ms": 100}
            )
            assert b._poll_interval_s(1, "fm") is None, "int caps: no subscription must be created"
            states_int = [m for m in col.sent if m["type"] == "state"]
            assert states_int == [], "int caps: no telemetry state must be emitted"
            # None capabilities
            await b.handle_subscribe(
                {"slot": 1, "module": "fm", "capabilities": None, "interval_ms": 100}
            )
            assert b._poll_interval_s(1, "fm") is None, (
                "None caps: no subscription must be created"
            )
            states_none = [m for m in col.sent if m["type"] == "state"]
            assert states_none == [], "None caps: no telemetry state must be emitted"
            await b.stop()

        _run(scenario())
    finally:
        fw.stop()


def test_unsubscribe_with_malformed_capabilities_does_not_raise_or_remove_real_caps():
    """handle_unsubscribe with malformed capabilities must not raise and must leave real caps."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(
            col,
            transport_factory=lambda p: SlotControl(p, timeout=2.0),
            telemetry_min_floor_ms=10,
            telemetry_default_interval_ms=100,
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            # Establish a real subscription.
            await b.handle_subscribe(
                {"slot": 1, "module": "fm", "capabilities": ["rssi"], "interval_ms": 100}
            )
            assert b._poll_interval_s(1, "fm") is not None, "subscription must be active"
            # Unsubscribe with None — must fail closed (caps unchanged).
            await b.handle_unsubscribe({"slot": 1, "module": "fm", "capabilities": None})
            # Real subscription must still be active.
            assert b._poll_interval_s(1, "fm") is not None, (
                "real subscription must survive malformed unsubscribe"
            )
            sub = b._subscriptions.get((1, "fm"))
            assert sub is not None and "rssi" in sub["caps"], "rssi cap must remain subscribed"
            await b.stop()

        _run(scenario())
    finally:
        fw.stop()


def test_stop_with_armed_ptt_completes_without_pending_task_warnings():
    """stop() after arming a PTT must complete without raising or leaving pending tasks."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(
            col,
            transport_factory=lambda p: SlotControl(p, timeout=2.0),
            dead_man_timeout=30.0,  # long: will not fire on its own
            telemetry_min_floor_ms=10,
            telemetry_default_interval_ms=100,
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            # Subscribe so stop() has both a subscription task and a PTT task to clean up.
            await b.handle_subscribe(
                {"slot": 1, "module": "fm", "capabilities": ["rssi"], "interval_ms": 100}
            )
            # Arm PTT.
            await b.handle(
                {
                    "v": 1,
                    "type": "command",
                    "request_id": "fw1",
                    "slot": 1,
                    "module": "fm",
                    "capability": "ptt",
                    "op": "do",
                    "value": True,
                }
            )
            assert b._ptt.get((1, "fm")) is not None, "PTT must be armed"
            # stop() must not raise and must drain both task sets.
            await b.stop()
            assert b._ptt == {}, "PTT registry must be empty after stop()"
            assert b._subscriptions == {}, "subscriptions must be empty after stop()"
            # No ptt_auto_unkey event — stop() must not unkey.
            events = [m for m in col.sent if m.get("event") == "ptt_auto_unkey"]
            assert events == [], "stop() must not emit ptt_auto_unkey"

        _run(scenario())
    finally:
        fw.stop()


def test_subscribe_streams_state_then_unsubscribe_stops():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        col = Collector()
        b = Broker(
            col,
            transport_factory=lambda p: SlotControl(p, timeout=2.0),
            telemetry_min_floor_ms=10,
            telemetry_default_interval_ms=20,
            now=lambda: 1.0,
        )
        b.set_inventory(
            [
                {
                    "slot": 1,
                    "control": fw.control_path,
                    "modules": [
                        {
                            "id": "fm",
                            "identity": FM["identity"],
                            "capabilities": FM["capabilities"],
                        }
                    ],
                }
            ]
        )

        async def scenario():
            await b.handle_subscribe(
                {"slot": 1, "module": "fm", "capabilities": ["rssi"], "interval_ms": 20}
            )
            await asyncio.sleep(0.12)  # a few ticks
            await b.handle_unsubscribe({"slot": 1, "module": "fm", "capabilities": ["rssi"]})
            count_after_unsub = len([m for m in col.sent if m["type"] == "state"])
            await asyncio.sleep(0.12)  # no more ticks should arrive
            final = len([m for m in col.sent if m["type"] == "state"])
            await b.stop()
            return count_after_unsub, final

        streamed, final = _run(scenario())
        assert streamed >= 1  # telemetry did stream
        assert final == streamed  # unsubscribe stopped the stream
    finally:
        fw.stop()
