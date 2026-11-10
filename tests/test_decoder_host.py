import asyncio

from station_agent.decoders.host import DecoderHost
from station_agent.decoders.spool import Spool
from station_agent.decoders.units import DecoderUnitError


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)

    def types(self):
        return [m["type"] for m in self.sent]


class FakeUnits:
    def __init__(self):
        self.calls = []
        self.fail_start = False
        self.unit_state = "active"

    def start(self, decoder, slot, env):
        self.calls.append(("start", decoder, slot, env))
        if self.fail_start:
            raise DecoderUnitError("boom")

    def stop(self, decoder, slot):
        self.calls.append(("stop", decoder, slot))

    def state(self, decoder, slot):
        return self.unit_state

    def stop_all(self):
        self.calls.append(("stop_all",))

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class FakePlugin:
    """Plugin whose run() blocks until told otherwise; emitted via ``emit_later``."""

    def __init__(self, name="aprs"):
        self.name = name
        self.emits = []
        self.raise_exc = None
        self.started = asyncio.Event()

    def env(self, slot, params, rx_node):
        return {"OE5XRX_SLOT": str(slot), "OE5XRX_RX_NODE": rx_node}

    async def run(self, slot, emit):
        self.started.set()
        for kind, reliable, data in self.emits:
            await emit(kind, reliable, data)
        if self.raise_exc:
            raise self.raise_exc
        await asyncio.Event().wait()


def _host(units=None, plugin=None, rx="node.rx", **kw):
    units = units or FakeUnits()
    plugin = plugin or FakePlugin()
    host = DecoderHost(
        units,
        {plugin.name: plugin},
        lambda slot: rx,
        spool=kw.pop("spool", Spool(epoch="E")),
        now=lambda: 50.0,
        **kw,
    )
    return host, units, plugin


async def _settle():
    # Unit calls run in an executor thread; give tasks real time to finish.
    await asyncio.sleep(0.1)


def run(coro):
    return asyncio.run(coro)


def test_host_stops_leftover_units_on_startup():
    async def go():
        host, units, _ = _host()
        await host.startup()
        assert units.named("stop_all") == [("stop_all",)]

    run(go())


def test_no_decoder_runs_without_start():
    async def go():
        host, units, _ = _host()
        await host.startup()
        await _settle()
        assert units.named("start") == [] and host.running() == []

    run(go())


def test_start_ok_sends_result_and_states():
    async def go():
        host, units, plugin = _host()
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        await _settle()
        assert c.types() == ["decoder_state", "result", "decoder_state"]
        assert [m.get("state") for m in c.sent if m["type"] == "decoder_state"] == [
            "starting",
            "running",
        ]
        assert c.sent[1]["request_id"] == "r1" and c.sent[1]["ok"] is True
        assert units.named("start") == [
            ("start", "aprs", 1, {"OE5XRX_SLOT": "1", "OE5XRX_RX_NODE": "node.rx"})
        ]
        assert host.running() == [{"decoder": "aprs", "slot": 1, "state": "running"}]
        await host.shutdown()

    run(go())


def test_start_unknown_decoder_error():
    async def go():
        host, units, _ = _host()
        c = Collector()
        host.attach(c)
        await host.start("nope", 1, {}, "r1")
        res = [m for m in c.sent if m["type"] == "result"][0]
        assert res["ok"] is False and res["error"]["code"] == "unknown_decoder"
        assert units.named("start") == []

    run(go())


def test_start_no_rx_node_error():
    async def go():
        host, units, _ = _host(rx=None)
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        res = [m for m in c.sent if m["type"] == "result"][0]
        assert res["error"]["code"] == "no_rx_node"
        assert units.calls == []

    run(go())


def test_start_unit_failure_reports_error():
    async def go():
        host, units, _ = _host()
        units.fail_start = True
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        res = [m for m in c.sent if m["type"] == "result"][0]
        assert res["error"]["code"] == "unit_failed"
        assert c.sent[-1]["state"] == "error" and host.running() == []

    run(go())


def test_start_same_twice_is_idempotent():
    async def go():
        host, units, _ = _host()
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        await host.start("aprs", 1, {}, "r2")
        assert len(units.named("start")) == 1
        results = [m for m in c.sent if m["type"] == "result"]
        assert [r["ok"] for r in results] == [True, True]
        await host.shutdown()

    run(go())


def test_start_other_decoder_on_slot_replaces():
    async def go():
        units = FakeUnits()
        a, b = FakePlugin("aprs"), FakePlugin("other")
        host = DecoderHost(units, {"aprs": a, "other": b}, lambda s: "n", spool=Spool())
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        await host.start("other", 1, {}, "r2")
        assert units.named("stop") == [("stop", "aprs", 1)]
        stopped = [m for m in c.sent if m.get("state") == "stopped"]
        assert stopped[0]["decoder"] == "aprs" and stopped[0]["detail"] == "replaced"
        assert host.running() == [{"decoder": "other", "slot": 1, "state": "running"}]
        await host.shutdown()

    run(go())


def test_stop_sends_result_and_stopped_state():
    async def go():
        host, units, _ = _host()
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        c.sent.clear()
        await host.stop("aprs", 1, "r2", "requested")
        assert units.named("stop") == [("stop", "aprs", 1)]
        assert [m["type"] for m in c.sent] == ["decoder_state", "result"]
        assert c.sent[0]["state"] == "stopped" and c.sent[0]["detail"] == "requested"
        assert c.sent[1]["ok"] is True
        assert host.running() == []

    run(go())


def test_stop_unknown_instance_is_ok():
    async def go():
        host, units, _ = _host()
        c = Collector()
        host.attach(c)
        await host.stop("aprs", 1, "r1")
        assert c.sent[0]["ok"] is True and units.calls == []

    run(go())


def test_unit_failed_health_reports_error_and_stops():
    async def go():
        host, units, _ = _host(health_poll_s=0)
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        units.unit_state = "failed"
        await _settle()
        assert c.sent[-1]["state"] == "error" and "failed" in c.sent[-1]["detail"]
        assert units.named("stop") == [("stop", "aprs", 1)]
        assert host.running() == []

    run(go())


def test_activating_counts_as_running():
    async def go():
        host, units, _ = _host(health_poll_s=0)
        units.unit_state = "activating"
        await host.start("aprs", 1, {}, "r1")
        await _settle()
        assert host.running() != []
        await host.shutdown()

    run(go())


def test_reader_connection_error_reports_error_and_stops():
    async def go():
        plugin = FakePlugin()
        plugin.raise_exc = ConnectionError("no kiss")
        host, units, _ = _host(plugin=plugin)
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        await _settle()
        assert c.sent[-1]["state"] == "error" and "no kiss" in c.sent[-1]["detail"]
        assert units.named("stop") == [("stop", "aprs", 1)]
        assert host.running() == []

    run(go())


def test_max_runtime_stops():
    async def go():
        gate = asyncio.Event()

        async def fake_sleep(s):
            if s == 60:
                await gate.wait()
            else:
                await asyncio.Event().wait()

        host, units, _ = _host(sleep=fake_sleep)
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1", max_runtime_s=60)
        gate.set()
        await _settle()
        assert c.sent[-1]["state"] == "stopped" and c.sent[-1]["detail"] == "max_runtime"
        assert units.named("stop") == [("stop", "aprs", 1)]

    run(go())


def test_reliable_output_spooled_while_detached_and_resent_on_connect():
    async def go():
        plugin = FakePlugin()
        plugin.emits = [("ax25", True, {"n": 1}), ("ax25", True, {"n": 2})]
        host, _, _ = _host(plugin=plugin)
        await host.start("aprs", 1, {}, "r1")
        await _settle()
        c = Collector()
        host.attach(c)
        await host.on_connected()
        assert c.types() == ["decoder_snapshot", "decoder_output", "decoder_output"]
        assert [m["seq"] for m in c.sent[1:]] == [1, 2]
        assert c.sent[0]["decoders"] == [{"decoder": "aprs", "slot": 1, "state": "running"}]
        assert c.sent[1]["epoch"] == "E" and c.sent[1]["data"]["n"] == 1
        await host.shutdown()

    run(go())


def test_ack_prevents_resend():
    async def go():
        plugin = FakePlugin()
        plugin.emits = [("ax25", True, {"n": 1}), ("ax25", True, {"n": 2})]
        host, _, _ = _host(plugin=plugin)
        await host.start("aprs", 1, {}, "r1")
        await _settle()
        await host.ack("E", [1])
        c = Collector()
        host.attach(c)
        await host.on_connected()
        assert [m["seq"] for m in c.sent if m["type"] == "decoder_output"] == [2]
        await host.shutdown()

    run(go())


def test_output_sent_live_when_attached():
    async def go():
        plugin = FakePlugin()
        plugin.emits = [("ax25", True, {"n": 1})]
        host, _, _ = _host(plugin=plugin)
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        await _settle()
        outs = [m for m in c.sent if m["type"] == "decoder_output"]
        assert len(outs) == 1 and outs[0]["reliable"] is True and outs[0]["ts"] == 50.0
        await host.shutdown()

    run(go())


def test_unreliable_output_dropped_while_detached():
    async def go():
        plugin = FakePlugin()
        plugin.emits = [("level", False, {"db": -20})]
        spool = Spool(epoch="E")
        host, _, _ = _host(plugin=plugin, spool=spool)
        await host.start("aprs", 1, {}, "r1")
        await _settle()
        assert spool.pending() == []
        c = Collector()
        host.attach(c)
        await host.on_connected()
        assert c.types() == ["decoder_snapshot"]
        await host.shutdown()

    run(go())


def test_send_failure_is_swallowed():
    async def go():
        host, units, _ = _host()

        async def bad(msg):
            raise RuntimeError("ws gone")

        host.attach(bad)
        await host.start("aprs", 1, {}, "r1")
        assert host.running() != []
        await host.shutdown()

    run(go())


def test_reconcile_stops_missing_slot():
    async def go():
        host, units, _ = _host()
        c = Collector()
        host.attach(c)
        await host.start("aprs", 1, {}, "r1")
        await host.reconcile_slots({2})
        stopped = [m for m in c.sent if m.get("state") == "stopped"]
        assert stopped[-1]["detail"] == "module_gone"
        assert units.named("stop") == [("stop", "aprs", 1)]

    run(go())


def test_reconcile_keeps_present_slot():
    async def go():
        host, units, _ = _host()
        await host.start("aprs", 1, {}, "r1")
        await host.reconcile_slots({1})
        assert units.named("stop") == []
        await host.shutdown()

    run(go())


def test_stop_slot():
    async def go():
        host, units, _ = _host()
        await host.start("aprs", 1, {}, "r1")
        await host.stop_slot(1, "preempted")
        assert host.running() == [] and units.named("stop") == [("stop", "aprs", 1)]

    run(go())
