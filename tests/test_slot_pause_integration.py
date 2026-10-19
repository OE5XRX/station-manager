"""Integration of the slot pause registry (module firmware update, Teil D) with every
control-device opener and the cross-thread hooks the module_ota cycle relies on.

While a slot is paused its control device must not be opened by the heartbeat inventory
scan, the control-plane rediscovery, commands or telemetry polls — and both inventories
report the last-known entry with ``"status": "updating"``.
"""

import asyncio
import json
import threading
import time
import types

import pytest
import websockets

from station_agent import slot_pause
from station_agent.audio.engine import AudioEngine
from station_agent.audio.ws_client import AudioClient
from station_agent.broker import Broker
from station_agent.control_client import ControlClient, _SlotInventoryDebouncer
from station_agent.devlock import control_device_lock
from station_agent.inventory import collect_inventory
from station_agent.slot_discovery import discover_slots
from tests.fake_fw import FakeFirmware, make_slot_tree
from tests.test_control_client import FM, _FakeConfig, _gen_key


@pytest.fixture(autouse=True)
def _reset_pause_registry():
    slot_pause._reset_for_tests()
    yield
    slot_pause._reset_for_tests()


def _entry(slot, control="/dev/oe5xrx/slot1/control"):
    return {
        "slot": slot,
        "control": control,
        "modules": [{"id": "fm", "identity": {"type": "fm_transceiver"}, "capabilities": []}],
    }


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


# --- slot_discovery / heartbeat inventory ---------------------------------------------


def test_discover_slots_skips_without_opening(tmp_path):
    fw1 = FakeFirmware({"fm": FM})
    fw2 = FakeFirmware({"fm": FM})
    fw1.start()
    fw2.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw1, 2: fw2})
        entries = discover_slots(base, skip={1})
    finally:
        fw1.stop()
        fw2.stop()
    assert [e["slot"] for e in entries] == [2]
    assert fw1.received == []


def test_heartbeat_inventory_keeps_paused_slot_as_updating(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw})
        slot_pause.pause(1, _entry(1, control=f"{base}/slot1/control"))
        config = types.SimpleNamespace(
            slot_discovery_enabled=True, slot_dev_base=base, trace_serial=False
        )
        modules = collect_inventory(config)["modules"]
    finally:
        fw.stop()
    assert [e["slot"] for e in modules] == [1]
    assert all(m["status"] == "updating" for m in modules[0]["modules"])
    assert not any(b"module list" in line for line in fw.received)


def test_heartbeat_inventory_paused_without_snapshot_is_omitted(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw})
        slot_pause.pause(1, None)
        config = types.SimpleNamespace(
            slot_discovery_enabled=True, slot_dev_base=base, trace_serial=False
        )
        modules = collect_inventory(config)["modules"]
    finally:
        fw.stop()
    assert modules == []
    assert fw.received == []


def test_heartbeat_inventory_slot_paused_mid_scan_is_not_probed(tmp_path, monkeypatch):
    """The pause state is checked right before each probe (not snapshotted before the
    scan), and the merge re-reads it: a slot paused while slot 1 is probed is neither
    opened nor reported from a stale probe."""
    from station_agent import slot_discovery

    fw1, fw2 = FakeFirmware({"fm": FM}), FakeFirmware({"fm": FM})
    fw1.start()
    fw2.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw1, 2: fw2})
        real_probe = slot_discovery.probe_slot
        probed = []

        def probe(control, *args, **kwargs):
            probed.append(control)
            if control.endswith("slot1/control"):
                slot_pause.pause(2, _entry(2, control=f"{base}/slot2/control"))
            return real_probe(control, *args, **kwargs)

        monkeypatch.setattr(slot_discovery, "probe_slot", probe)
        config = types.SimpleNamespace(
            slot_discovery_enabled=True, slot_dev_base=base, trace_serial=False
        )
        modules = collect_inventory(config)["modules"]
    finally:
        fw1.stop()
        fw2.stop()
    assert probed == [f"{base}/slot1/control"]
    assert fw2.received == []
    assert [e["slot"] for e in modules] == [1, 2]
    assert all(m["status"] == "updating" for m in modules[1]["modules"])


# --- debouncer ---------------------------------------------------------------------------


def test_debouncer_never_drops_paused_slot():
    deb = _SlotInventoryDebouncer(max_misses=2)
    assert deb.update([_entry(1)]) == [_entry(1)]
    slot_pause.pause(1, _entry(1))
    for _ in range(10):
        effective = deb.update([])
        assert [e["slot"] for e in effective] == [1]
        assert effective[0]["modules"][0]["status"] == "updating"
    # The retained last-known-good entry is the raw one, never the updating view.
    assert "status" not in deb.last_good(1)["modules"][0]
    slot_pause.resume(1)
    # After resume, the miss counter starts fresh: still retained for max_misses cycles.
    assert [e["slot"] for e in deb.update([])] == [1]
    assert "status" not in deb.update([])[0]["modules"][0]
    assert deb.update([]) == []


# --- broker ------------------------------------------------------------------------------


def test_broker_execute_paused_returns_updating_fast():
    path = "/dev/oe5xrx/slot1/control"
    built = []

    def factory(p):
        built.append(p)
        raise AssertionError("no transport may be built for a paused slot")

    broker = Broker(Collector(), transport_factory=factory)
    broker.set_inventory([_entry(1, control=path)])
    slot_pause.pause(1, _entry(1, control=path))

    async def scenario():
        start = time.monotonic()
        result = await broker._execute(1, "fm", "get", "rssi", None)
        return result, time.monotonic() - start

    # The device lock is held by this (test) thread: a paused slot must not even try it.
    with control_device_lock(path):
        result, elapsed = asyncio.run(scenario())
    assert result == {"ok": False, "error": "updating"}
    assert elapsed < 0.1
    assert built == []
    assert 1 not in broker._slot_locks


def test_broker_emit_inventory_paused_slot_skips_gets_and_marks_updating():
    def factory(_p):
        raise AssertionError("no transport may be built for a paused slot")

    sent = Collector()
    broker = Broker(sent, transport_factory=factory)
    entry = {
        "slot": 1,
        "control": "/dev/oe5xrx/slot1/control",
        "modules": [
            {
                "id": "fm",
                "identity": {"type": "fm_transceiver"},
                "capabilities": [{"name": "frequency", "kind": "setting", "type": "float"}],
            }
        ],
    }
    slot_pause.pause(1, entry)
    broker.set_inventory([slot_pause.updating_entry(1)])
    asyncio.run(broker.emit_inventory())
    (inv,) = sent.sent
    module = inv["slots"][0]["modules"][0]
    assert module["state"] == {}
    assert module["status"] == "updating"


def test_broker_slot_busy_tracks_ptt():
    broker = Broker(Collector())
    assert broker.slot_busy(1) is False
    broker._ptt[(1, "fm")] = {"cap": "ptt", "task": None}
    assert broker.slot_busy(1) is True
    assert broker.slot_busy(2) is False
    broker._ptt.clear()
    assert broker.slot_busy(1) is False


def test_broker_slot_busy_during_failsafe_unkey():
    seen = {}

    class Transport:
        def execute(self, module, op, capability, token, trace):
            # Mid-unkey: _ptt is already popped, but the slot must still read busy.
            # (Read from the executor thread here only for the test — a set membership
            # read is atomic; production callers go through ControlClient.slot_busy.)
            seen["busy"] = broker.slot_busy(1)
            return {"ok": True, "value": token}

    broker = Broker(Collector(), transport_factory=lambda _p: Transport())
    broker.set_inventory([_entry(1)])
    asyncio.run(broker._unkey(1, "fm", "ptt", "keepalive_timeout"))
    assert seen["busy"] is True
    assert broker.slot_busy(1) is False


_PTT_ENTRY = {
    "slot": 1,
    "control": "/dev/oe5xrx/slot1/control",
    "modules": [
        {
            "id": "fm",
            "identity": {"type": "fm_transceiver"},
            "capabilities": [{"name": "ptt", "kind": "action", "type": "bool"}],
        }
    ],
}


def test_broker_slot_busy_while_ptt_key_in_flight():
    seen = {}

    class Transport:
        def execute(self, module, op, capability, token, trace):
            # `do ptt true` on the wire, dead-man not yet armed: must already read busy.
            seen["busy"] = broker.slot_busy(1)
            return {"ok": True, "value": token}

    broker = Broker(Collector(), transport_factory=lambda _p: Transport())
    broker.set_inventory([_PTT_ENTRY])
    cmd = {
        "type": "command",
        "request_id": "k1",
        "slot": 1,
        "module": "fm",
        "capability": "ptt",
        "op": "do",
        "value": True,
    }

    async def scenario():
        await broker.handle_command(cmd)
        busy_after = broker.slot_busy(1)
        await broker.stop()
        return busy_after

    assert asyncio.run(scenario()) is True  # dead-man armed => still busy
    assert seen["busy"] is True
    assert broker._ptt_inflight == set()


def test_failsafe_unkey_on_paused_slot_reaches_device(tmp_path):
    fw = FakeFirmware({"fm": {**FM, "capabilities": _PTT_ENTRY["modules"][0]["capabilities"]}})
    fw.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw})
        entry = {**_PTT_ENTRY, "control": f"{base}/slot1/control"}
        sent = Collector()
        broker = Broker(sent)
        broker.set_inventory([entry])
        slot_pause.pause(1, entry)
        # A normal command on the paused slot is refused ...
        assert asyncio.run(broker._execute(1, "fm", "do", "ptt", "true")) == {
            "ok": False,
            "error": "updating",
        }
        # ... but the fail-safe unkey (dead-man / disconnect) always reaches the device.
        asyncio.run(broker._unkey(1, "fm", "ptt", "keepalive_timeout"))
    finally:
        fw.stop()
    assert fw.state["fm"].get("ptt") == "false"
    assert any(b"module fm do ptt false" in line for line in fw.received)
    assert sent.sent[-1]["event"] == "ptt_auto_unkey"


def test_rediscovery_interval_zero_still_honours_refresh(tmp_path):
    cfg = _FakeConfig("http://127.0.0.1:1", 1, _gen_key(tmp_path), str(tmp_path))
    cfg.control_rediscovery_interval = 0
    client = ControlClient(cfg)

    class FakeBroker:
        def __init__(self):
            self.scans = 0
            self.emitted = asyncio.Event()

        async def rediscover(self, _fn):
            self.scans += 1
            return [_entry(1)]

        def set_inventory(self, _inv):
            pass

        async def emit_inventory(self):
            self.emitted.set()

    async def scenario():
        fake = FakeBroker()
        client._ws = object()
        client._refresh_event = asyncio.Event()
        client._loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            client._rediscovery_loop(fake, client._loop, _SlotInventoryDebouncer(), [])
        )
        await asyncio.sleep(0.05)
        assert fake.scans == 0  # no periodic scan with interval 0
        client.request_inventory_refresh()
        await asyncio.wait_for(fake.emitted.wait(), timeout=2.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return fake.scans

    assert asyncio.run(scenario()) == 1


def _live_skip_probe(monkeypatch, seen):
    """Patch control_client.discover_slots: pause slot 2 mid-scan, then record whether
    the passed ``skip`` sees it (a frozen snapshot would not)."""
    from station_agent import control_client

    def fake_discover(base, trace=False, skip=()):
        assert 2 not in skip
        slot_pause.pause(2, _entry(2))
        seen.append(2 in skip)
        return [_entry(1)]

    monkeypatch.setattr(control_client, "discover_slots", fake_discover)


def test_rediscovery_checks_pause_state_live(tmp_path, monkeypatch):
    seen = []
    _live_skip_probe(monkeypatch, seen)
    cfg = _FakeConfig("http://127.0.0.1:1", 1, _gen_key(tmp_path), str(tmp_path))
    cfg.control_rediscovery_interval = 0
    client = ControlClient(cfg)

    class FakeBroker:
        def __init__(self):
            self.emitted = asyncio.Event()

        async def rediscover(self, fn):
            return fn()

        def set_inventory(self, _inv):
            pass

        async def emit_inventory(self):
            self.emitted.set()

    async def scenario():
        fake = FakeBroker()
        client._ws = object()
        client._refresh_event = asyncio.Event()
        client._loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            client._rediscovery_loop(fake, client._loop, _SlotInventoryDebouncer(), [])
        )
        client.request_inventory_refresh()
        await asyncio.wait_for(fake.emitted.wait(), timeout=2.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert seen == [True]


def test_connect_discovery_checks_pause_state_live(tmp_path, monkeypatch):
    seen = []
    _live_skip_probe(monkeypatch, seen)
    key_path = _gen_key(tmp_path)
    got = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            if json.loads(raw)["type"] == "inventory":
                got.set()

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, str(tmp_path))
            client = ControlClient(cfg)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(got.wait(), timeout=8.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    asyncio.run(scenario())
    assert seen[0] is True


def test_broker_firmware_check_calls_callback():
    calls = []
    broker = Broker(Collector(), on_firmware_check=lambda: calls.append(1))
    asyncio.run(broker.handle({"v": 1, "type": "firmware_check"}))
    assert calls == [1]
    # Without a callback the frame is simply ignored.
    asyncio.run(Broker(Collector()).handle({"v": 1, "type": "firmware_check"}))


# --- audio ------------------------------------------------------------------------------


def test_audio_engine_slot_active():
    async def emit_json(_msg):
        pass

    eng = AudioEngine(object(), emit_json=emit_json, emit_binary=lambda _d: None)
    assert eng.slot_active(1) is False
    # RX-only listening never makes a slot busy (final-review ruling: a passive viewer
    # must not postpone a flash forever while the drain locks everyone out).
    eng._rx["slot1.rx"] = {}
    assert eng.slot_active(1) is False
    assert eng.slot_active(2) is False
    eng._tx = {"bridge": None, "slot": 2, "module": "fm"}
    assert eng.slot_active(2) is True
    assert eng.slot_active(1) is False
    eng._rx["slot2.rx"] = {}
    assert eng.slot_active(2) is True  # TX + RX: still busy because of TX
    eng._rx.clear()
    eng._tx = None
    eng._diag = {"bridge": None, "slot": 3, "ref": 1}
    assert eng.slot_active(3) is True
    assert eng.slot_active(1) is False


def test_audio_client_slot_active_false_without_engine():
    client = AudioClient.__new__(AudioClient)
    client._engine = None
    assert client.slot_active(1) is False


# --- control client cross-thread hooks ------------------------------------------------


def test_control_client_slot_busy_false_without_broker_true_on_timeout(tmp_path):
    cfg = _FakeConfig("http://127.0.0.1:1", 1, _gen_key(tmp_path), str(tmp_path))
    client = ControlClient(cfg)
    # Not connected: no broker, no loop => no PTT possible => not busy.
    assert client.slot_busy(1) is False
    # Disconnected hooks are no-ops and never raise.
    client.send_event(1, "fm", "firmware_update_started", {})
    client.request_inventory_refresh()
    assert client.last_inventory_entry(1) is None

    # A broker on a loop that never answers => the cross-thread call times out => busy.
    loop = asyncio.new_event_loop()
    blocker = threading.Event()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        loop.call_soon_threadsafe(blocker.wait)  # wedge the loop
        client._loop = loop
        client._broker = Broker(Collector())
        start = time.monotonic()
        assert client.slot_busy(1, timeout=0.2) is True
        assert time.monotonic() - start < 2.0
    finally:
        blocker.set()
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()


def test_request_inventory_refresh_emits_updating_status(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = make_slot_tree(tmp_path, {1: fw})
    key_path = _gen_key(tmp_path)

    inventories = []
    second = asyncio.Event()
    holder = {}

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "inventory":
                inventories.append(msg)
                if len(inventories) == 1:
                    client = holder["client"]
                    snap = client.last_inventory_entry(1)
                    assert snap is not None and snap["slot"] == 1
                    slot_pause.pause(1, snap)
                    lines_before = len(fw.received)
                    holder["lines_before"] = lines_before
                    # Called from a foreign thread, as the module_ota cycle does.
                    threading.Thread(target=client.request_inventory_refresh).start()
                elif len(inventories) == 2:
                    second.set()

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            client = ControlClient(cfg)  # rediscovery interval 30 s: only the refresh can fire
            holder["client"] = client
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(second.wait(), timeout=8.0)
                # While connected, PTT is idle on slot 1.
                busy = await loop.run_in_executor(None, client.slot_busy, 1)
                assert busy is False
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()

    first_mod = inventories[0]["slots"][0]["modules"][0]
    assert "status" not in first_mod
    slot1 = inventories[1]["slots"][0]
    assert slot1["slot"] == 1
    assert slot1["modules"][0]["status"] == "updating"
    assert slot1["modules"][0]["state"] == {}
    # The paused slot's device was never touched after the pause.
    assert fw.received[holder["lines_before"] :] == []


def test_command_queued_behind_slot_lock_never_opens_paused_slot():
    """TOCTOU: a poll/command that passed the early pause check and queued behind an
    in-flight opener on the slot lock must re-check after acquiring it — the flasher's
    barrier only drains the in-flight opener, not calls queued behind it."""
    calls = []
    release_first = threading.Event()
    entered_first = threading.Event()

    class Transport:
        def execute(self, module, op, capability, token, trace):
            calls.append(capability)
            if len(calls) == 1:
                entered_first.set()
                release_first.wait(5)
            return {"ok": True, "value": "x"}

    broker = Broker(Collector(), transport_factory=lambda _p: Transport())
    broker.set_inventory([_PTT_ENTRY])

    async def scenario():
        first = asyncio.ensure_future(broker._execute(1, "fm", "get", "first", None))
        while not entered_first.is_set():
            await asyncio.sleep(0.005)
        second = asyncio.ensure_future(broker._execute(1, "fm", "get", "second", None))
        await asyncio.sleep(0.02)  # second passed the early check, waits on the slot lock
        slot_pause.pause(1, _PTT_ENTRY)  # module_ota pauses the slot meanwhile
        release_first.set()
        return await first, await second

    first_result, second_result = asyncio.run(scenario())
    assert first_result["ok"] is True
    assert second_result == {"ok": False, "error": "updating"}
    assert calls == ["first"]  # the queued call never reached the device


def test_slot_control_abort_if_checked_under_devlock_before_open(tmp_path, monkeypatch):
    """SlotControl evaluates abort_if after taking the devlock and never opens the device
    when it is true; the broker wires the pause check in for real SlotControl transports
    (and not for the failsafe unkey)."""
    from station_agent import slot_control
    from station_agent.slot_control import SlotControl

    opened = []
    real_open = slot_control.os.open

    def spy_open(path, *a, **kw):
        opened.append(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(slot_control.os, "open", spy_open)
    path = str(tmp_path / "control")
    seen_locked = []

    def abort_if():
        # The devlock is held here: a non-blocking acquire from this thread must fail.
        from station_agent.devlock import _lock_for

        lock = _lock_for(path)
        got = lock.acquire(blocking=False)
        if got:
            lock.release()
        seen_locked.append(not got)
        return True

    sc = SlotControl(path, timeout=0.1)
    assert sc.execute("fm", "get", "freq", abort_if=abort_if) == {
        "ok": False,
        "error": "updating",
    }
    assert seen_locked == [True]
    assert opened == []

    seen = {}

    class SpySlotControl(SlotControl):
        def execute(self, *args, abort_if=None, **kw):
            seen["abort_if"] = abort_if
            return {"ok": True, "value": "1"}

    broker = Broker(Collector(), transport_factory=lambda p: SpySlotControl(p))
    broker.set_inventory([_PTT_ENTRY])
    assert asyncio.run(broker._execute(1, "fm", "get", "freq", None))["ok"] is True
    assert seen["abort_if"] is not None and seen["abort_if"]() is False
    slot_pause.pause(1, _PTT_ENTRY)
    assert seen["abort_if"]() is True
    asyncio.run(broker._execute(1, "fm", "do", "ptt", "false", failsafe=True))
    assert seen["abort_if"] is None


def test_debouncer_forget_drops_last_good_immediately():
    deb = _SlotInventoryDebouncer(max_misses=2)
    deb.update([_entry(1)])
    deb.forget(1)
    assert deb.update([]) == []  # no stale re-emit within the miss budget
    assert deb.last_good(1) is None
    assert deb.update([_entry(1)]) == [_entry(1)]  # a genuine re-find is reported again


def test_forget_slot_after_dfu_stuck_omits_stale_entry_from_next_emit(tmp_path):
    """Review #168 r4: after a flash ends DFU_STUCK, module_ota resumes the slot and
    refreshes; the module (in DFU, no CDC) is invisible to the scan. Without forget_slot
    the debouncer re-emits the stale last-known identity (miss 1/2), which the server
    reads as "back online" and clears the DFU-stuck notice."""
    cfg = _FakeConfig("http://127.0.0.1:1", 1, _gen_key(tmp_path), str(tmp_path))
    cfg.control_rediscovery_interval = 0
    client = ControlClient(cfg)

    class FakeBroker:
        def __init__(self):
            self.inventories = []
            self.emitted = asyncio.Event()

        async def rediscover(self, _fn):
            return []  # the module hangs in DFU mode: no CDC control device

        def set_inventory(self, inv):
            self.inventories.append(inv)

        async def emit_inventory(self):
            self.emitted.set()

    async def scenario():
        fake = FakeBroker()
        deb = _SlotInventoryDebouncer(max_misses=2)
        before = deb.update([_entry(1)])  # pre-flash inventory: module present
        client._publish_last_good(deb)
        client._ws = object()
        client._refresh_event = asyncio.Event()
        client._loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(client._rediscovery_loop(fake, client._loop, deb, before))
        client.forget_slot(1)  # module_ota: flash cycle ended DFU_STUCK
        client.request_inventory_refresh()
        await asyncio.wait_for(fake.emitted.wait(), timeout=2.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return fake.inventories

    inventories = asyncio.run(scenario())
    assert inventories == [[]]  # slot omitted, not the stale identity
    assert client.last_inventory_entry(1) is None


def test_forced_refresh_reemits_unchanged_inventory(tmp_path):
    """Fast-fail module_ota cycle: pause -> download fails in < 1 s -> resume completes
    between two scans, so the debounced inventory never changed. The end-of-cycle
    refresh is forced and must still emit a post-finish inventory frame (the server
    soft-offlined the row on firmware_update_finished); a plain refresh must not."""
    cfg = _FakeConfig("http://127.0.0.1:1", 1, _gen_key(tmp_path), str(tmp_path))
    cfg.control_rediscovery_interval = 0
    client = ControlClient(cfg)

    class FakeBroker:
        def __init__(self):
            self.scans = 0
            self.inventories = []
            self.scanned = asyncio.Event()

        async def rediscover(self, _fn):
            self.scans += 1
            self.scanned.set()
            return [_entry(1)]

        def set_inventory(self, inv):
            self.inventories.append(inv)

        async def emit_inventory(self):
            pass

    async def scenario():
        fake = FakeBroker()
        deb = _SlotInventoryDebouncer()
        before = deb.update([_entry(1)])
        client._ws = object()
        client._refresh_event = asyncio.Event()
        client._loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(client._rediscovery_loop(fake, client._loop, deb, before))
        # Plain refresh with an unchanged inventory: scanned, nothing emitted.
        client.request_inventory_refresh()
        await asyncio.wait_for(fake.scanned.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        plain = list(fake.inventories)
        # Forced refresh (module_ota _finish): re-emitted although unchanged.
        fake.scanned.clear()
        threading.Thread(target=lambda: client.request_inventory_refresh(force=True)).start()
        await asyncio.wait_for(fake.scanned.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return plain, fake.inventories, before

    plain, inventories, before = asyncio.run(scenario())
    assert plain == []
    assert inventories == [before]
    assert client._force_emit is False  # consumed by the forced cycle
