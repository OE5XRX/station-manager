# tests/test_audio_slot_hotplug.py
"""Audio-slot hot-plug: re-detect audio slots after the agent started.

Regression (test station 2026-11-06): the fm_board was moved from slot 2 to slot 3. The
control inventory re-discovered it, but the audio channel kept advertising ``slot2.rx`` until
the agent was restarted, so the Web UI's RX subscription hung in ERROR.

Covered here:
- ``AudioEngine.refresh`` — re-advertise on a changed slot set, stop streams of vanished
  slots with a clear ``idle`` state, restart streams whose slot re-enumerated / ref moved /
  bridge died, tear down TX on a vanished or re-enumerated slot.
- ``AudioSupervisor`` — debounced slot snapshot (slot → card generation); 2→3, 0→1, 1→0,
  same slot re-enumerated (DFU), flap ignored; owns the audio client + router lifecycle.
- ``PipeWireRouterBackend.audio_slot_generations`` / ``RxBridge.is_alive``.
- ``ControlClient`` re-discovery hook + dynamic virtual modules, ``Broker.set_virtual_modules``.
"""

from __future__ import annotations

import asyncio
import os
import threading

from station_agent.audio.engine import AudioEngine
from station_agent.audio.router_backend import PipeWireRouterBackend, RunResult
from station_agent.audio.supervisor import AudioSupervisor

# --- engine fakes ------------------------------------------------------------------------


class FakeBackend:
    def __init__(self, slots, nodes=None):
        self.slots = list(slots)
        self.nodes = nodes or {}

    def list_audio_slots(self):
        return list(self.slots)

    def resolve_node(self, slot, direction):
        return self.nodes.get((slot, direction))


class FakeRx:
    def __init__(self, node, rate, on_opus):
        self.node, self.rate, self.on_opus = node, rate, on_opus
        self.started = self.stopped = False
        self.alive = True

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def is_alive(self):
        return self.alive and not self.stopped


class FakeTx:
    def __init__(self, node, rate):
        self.node, self.rate = node, rate
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def feed_opus(self, payload):
        pass

    def stop(self):
        self.stopped = True


class FakeFactory:
    def __init__(self):
        self.rx = []
        self.tx = []

    def make_rx(self, node, rate, on_opus):
        b = FakeRx(node, rate, on_opus)
        self.rx.append(b)
        return b

    def make_tx(self, node, rate, on_meter=None):
        b = FakeTx(node, rate)
        self.tx.append(b)
        return b


def make_engine(slots, nodes):
    backend = FakeBackend(slots, nodes)
    factory = FakeFactory()
    sent = []

    async def emit_json(msg):
        sent.append(msg)

    eng = AudioEngine(
        backend,
        bridge_factory=factory,
        emit_json=emit_json,
        emit_binary=lambda data: None,
        dead_man_timeout=5.0,
    )
    return eng, backend, factory, sent


def _adverts(sent):
    return [m for m in sent if m["type"] == "advertise"]


def _states(sent, sid):
    return [m for m in sent if m["type"] == "stream_state" and m["stream_id"] == sid]


NODES = {
    (2, "rx"): "fm.slot2",
    (3, "rx"): "fm.slot3",
    (2, "tx"): "fm.slot2.sink",
    (3, "tx"): "fm.slot3.sink",
}


# --- AudioEngine.refresh -----------------------------------------------------------------


def test_refresh_slot_moved_2_to_3_readvertises_and_idles_old_stream():
    eng, backend, factory, sent = make_engine([2], NODES)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot2.rx")
        backend.slots = [3]
        await eng.refresh([3])

    asyncio.run(scenario())
    adv = _adverts(sent)
    assert len(adv) == 2
    assert {s["stream_id"] for s in adv[-1]["streams"]} == {"slot3.rx", "op.mic"}
    assert factory.rx[0].stopped
    last = _states(sent, "slot2.rx")[-1]
    assert last["state"] == "idle" and "removed" in last["detail"]
    assert "slot2.rx" not in eng._rx


def test_refresh_unchanged_set_is_a_noop():
    eng, _, factory, sent = make_engine([2], NODES)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot2.rx")
        before = len(sent)
        await eng.refresh([2])
        return before

    before = asyncio.run(scenario())
    assert len(sent) == before  # no re-advertise, no stream_state churn
    assert len(factory.rx) == 1 and not factory.rx[0].stopped


def test_refresh_reenumerated_slot_restarts_rx_bridge_on_same_stream():
    """DFU re-enumerates the board in the same slot: same stream set, but the old PipeWire
    node is gone — the RX pipeline must be rebuilt (no re-advertise needed)."""
    eng, _, factory, sent = make_engine([2], NODES)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot2.rx")
        await eng.refresh([2], rebuild={2})

    asyncio.run(scenario())
    assert len(_adverts(sent)) == 1
    assert len(factory.rx) == 2
    assert factory.rx[0].stopped and factory.rx[1].started and not factory.rx[1].stopped
    assert eng._rx["slot2.rx"]["bridge"] is factory.rx[1]
    assert _states(sent, "slot2.rx")[-1]["state"] == "live"


def test_refresh_restarts_dead_rx_bridge():
    eng, _, factory, _ = make_engine([2], NODES)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot2.rx")
        factory.rx[0].alive = False  # gst pipeline exited (node vanished)
        await eng.refresh([2])

    asyncio.run(scenario())
    assert len(factory.rx) == 2
    assert eng._rx["slot2.rx"]["bridge"] is factory.rx[1]


def test_refresh_ref_shift_restarts_surviving_stream_after_advertise():
    """[2,3] → [3]: slot3.rx keeps streaming but its stream_ref moves 1 → 0. A frame with
    the new ref must never precede the advertise that maps it, so the bridge restarts after."""
    eng, _, factory, sent = make_engine([2, 3], NODES)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot3.rx")
        old_ref = eng._rx["slot3.rx"]["ref"]
        await eng.refresh([3])
        return old_ref

    old_ref = asyncio.run(scenario())
    new_ref = eng.registry.ref_for("slot3.rx")
    assert old_ref != new_ref
    assert eng._rx["slot3.rx"]["ref"] == new_ref
    assert factory.rx[0].stopped and eng._rx["slot3.rx"]["bridge"] is factory.rx[1]
    adv_idx = max(i for i, m in enumerate(sent) if m["type"] == "advertise")
    live_idx = max(
        i
        for i, m in enumerate(sent)
        if m.get("stream_id") == "slot3.rx" and m["type"] == "stream_state"
    )
    assert adv_idx < live_idx


def test_refresh_0_to_1_advertises_new_slot():
    eng, _, _, sent = make_engine([], NODES)

    async def scenario():
        await eng.start()
        await eng.refresh([3])

    asyncio.run(scenario())
    assert {s["stream_id"] for s in _adverts(sent)[-1]["streams"]} == {"slot3.rx", "op.mic"}


def test_refresh_tears_down_tx_on_vanished_slot():
    eng, _, factory, sent = make_engine([2], NODES)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=2, tx_module="fm")
        await eng.refresh([3])

    asyncio.run(scenario())
    assert factory.tx[0].stopped
    assert eng._tx is None
    assert any(m["type"] == "tx_meter" and m["active"] is False for m in sent)


def test_refresh_tears_down_tx_on_reenumerated_slot_and_keepalive_restarts():
    eng, _, factory, _ = make_engine([2], NODES)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=2, tx_module="fm")
        await eng.refresh([2], rebuild={2})
        assert eng._tx is None
        await eng.on_mic_state(active=True, tx_slot=2, tx_module="fm")  # server keepalive

    asyncio.run(scenario())
    assert factory.tx[0].stopped
    assert len(factory.tx) == 2 and factory.tx[1].started


def test_refresh_keeps_tx_on_untouched_slot():
    eng, _, factory, _ = make_engine([2, 3], NODES)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=3, tx_module="fm")
        await eng.refresh([3])

    asyncio.run(scenario())
    assert not factory.tx[0].stopped
    assert eng._tx is not None


def test_concurrent_subscribe_during_refresh_starts_one_bridge():
    """The server re-sends source_subscribe for demanded streams on every advertise; that
    must not race the engine's own restart into two bridges for one stream."""
    eng, _, factory, _ = make_engine([2, 3], NODES)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot3.rx")
        await asyncio.gather(eng.refresh([3]), eng.on_source_subscribe("slot3.rx"))

    asyncio.run(scenario())
    live = [b for b in factory.rx if not b.stopped]
    assert len(live) == 1


# --- RxBridge.is_alive -------------------------------------------------------------------


def test_rx_bridge_is_alive_tracks_process():
    from station_agent.audio.opus_bridge import RxBridge

    class Proc:
        def __init__(self):
            self.rc = None

        def poll(self):
            return self.rc

        def terminate(self):
            self.rc = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.rc = -9

    class Sock:
        def bind(self, addr):
            pass

        def close(self):
            pass

    proc = Proc()
    b = RxBridge(
        "n",
        47000,
        8000,
        lambda p: None,
        spawn=lambda argv: proc,
        socket_factory=Sock,
        start_reader=False,
    )
    assert b.is_alive() is False
    b.start()
    assert b.is_alive() is True
    proc.rc = 1
    assert b.is_alive() is False


# --- backend generations -----------------------------------------------------------------


def test_backend_audio_slot_generations_change_on_reenumeration(tmp_path):
    sysfs = tmp_path / "sound"
    (sysfs / "card1").mkdir(parents=True)
    (sysfs / "card0").mkdir()

    def run(argv, timeout=None):
        path = argv[-1]
        if path.endswith("card1"):
            return RunResult(0, "OE5XRX_SLOT=2\n", "")
        return RunResult(0, "ID_FOO=1\n", "")

    be = PipeWireRouterBackend(run=run, sysfs_sound=str(sysfs))
    gen1 = be.audio_slot_generations()
    assert list(gen1) == [2]
    assert gen1[2][0] == 1
    # Re-enumeration: the card directory is recreated (new kernfs node). Keep an extra dir
    # around so the filesystem cannot hand the freed inode straight back.
    (sysfs / "keep").mkdir()
    os.rmdir(sysfs / "card1")
    (sysfs / "card1").mkdir()
    gen2 = be.audio_slot_generations()
    assert list(gen2) == [2]
    assert gen2 != gen1


# --- AudioSupervisor ---------------------------------------------------------------------


class ScanBackend:
    """Supervisor backend: returns successive snapshots (the last one repeats)."""

    def __init__(self, *snaps):
        self.snaps = list(snaps)
        self.calls = 0

    def audio_slot_generations(self):
        i = min(self.calls, len(self.snaps) - 1)
        self.calls += 1
        snap = self.snaps[i]
        if isinstance(snap, Exception):
            raise snap
        return dict(snap)


class FakeClient:
    def __init__(self):
        self.ran = threading.Event()
        self.stopped = threading.Event()
        self.updates = []

    def run(self):
        self.ran.set()
        self.stopped.wait(5)

    def stop(self):
        self.stopped.set()

    def update_slots(self, slots, rebuild=frozenset()):
        self.updates.append((list(slots), set(rebuild)))

    def slot_active(self, slot):
        return slot == 3


class FakeControl:
    def __init__(self):
        self.virtual = []

    def set_virtual_modules(self, vms):
        self.virtual.append(list(vms))


def make_supervisor(backend):
    clients = []

    def client_factory():
        c = FakeClient()
        clients.append(c)
        return c

    routers = []

    def router_factory():
        r = object()
        routers.append(r)
        return r

    sup = AudioSupervisor(
        backend,
        client_factory=client_factory,
        router_factory=router_factory,
        settle_s=0.0,
        confirm=2,
        wait=lambda timeout: False,
    )
    return sup, clients, routers


def test_supervisor_initial_scan_builds_router_and_starts_client():
    sup, clients, routers = make_supervisor(ScanBackend({2: (1, 10)}))
    assert sup.initial_scan() == [2]
    assert sup.router_module is routers[0]
    control = FakeControl()
    sup.start(control)
    try:
        assert clients and clients[0].ran.wait(2)
        assert control.virtual == []  # router went in via the ControlClient constructor
    finally:
        sup.stop()
    assert clients[0].stopped.is_set()


def test_supervisor_initial_scan_failure_is_fail_closed():
    sup, clients, _ = make_supervisor(ScanBackend(OSError("no sysfs")))
    assert sup.initial_scan() == []
    assert sup.router_module is None
    sup.start(FakeControl())
    sup.stop()
    assert clients == []


def test_supervisor_slot_moved_2_to_3_updates_running_client():
    be = ScanBackend({2: (1, 10)}, {3: (1, 11)})
    sup, clients, _ = make_supervisor(be)
    sup.initial_scan()
    sup.start(FakeControl())
    try:
        assert sup.check() is True
        assert clients[0].updates[-1] == ([3], set())
        assert len(clients) == 1  # no client restart for a non-empty → non-empty change
    finally:
        sup.stop()


def test_supervisor_0_to_1_starts_audio_and_registers_router():
    be = ScanBackend({}, {3: (1, 11)})
    sup, clients, routers = make_supervisor(be)
    assert sup.initial_scan() == []
    control = FakeControl()
    sup.start(control)
    try:
        assert clients == []
        assert sup.check() is True
        assert len(clients) == 1 and clients[0].ran.wait(2)
        assert control.virtual[-1] == [routers[0]]
        assert sup.slot_active(3) is True
    finally:
        sup.stop()


def test_supervisor_1_to_0_stops_audio_and_drops_router():
    be = ScanBackend({3: (1, 11)}, {})
    sup, clients, _ = make_supervisor(be)
    sup.initial_scan()
    control = FakeControl()
    sup.start(control)
    try:
        assert clients[0].ran.wait(2)
        assert sup.check() is True
        assert clients[0].stopped.is_set()
        assert control.virtual[-1] == []
        assert sup.slot_active(3) is False
    finally:
        sup.stop()


def test_supervisor_reenumerated_same_slot_requests_rebuild():
    be = ScanBackend({2: (1, 10)}, {2: (1, 99)})
    sup, clients, _ = make_supervisor(be)
    sup.initial_scan()
    sup.start(FakeControl())
    try:
        assert sup.check() is True
        assert clients[0].updates[-1] == ([2], {2})
    finally:
        sup.stop()


def test_supervisor_debounces_a_flap():
    """One scan sees the slot missing (re-enumeration in progress), the confirm scan sees it
    back unchanged → no change applied, audio keeps running."""
    be = ScanBackend({2: (1, 10)}, {}, {2: (1, 10)})
    sup, clients, _ = make_supervisor(be)
    sup.initial_scan()
    sup.start(FakeControl())
    try:
        assert sup.check() is False
        assert not clients[0].stopped.is_set()
        assert all(u == ([2], set()) for u in clients[0].updates)
    finally:
        sup.stop()


def test_supervisor_gives_up_on_persistent_flapping():
    be = ScanBackend({2: (1, 10)}, *([{}, {3: (1, 1)}] * 10))
    sup, clients, _ = make_supervisor(be)
    sup.initial_scan()
    sup.start(FakeControl())
    try:
        assert sup.check() is False
        assert be.calls <= 2 + sup.max_rounds
        assert not clients[0].stopped.is_set()
    finally:
        sup.stop()


def test_supervisor_unchanged_scan_still_lets_client_heal_dead_bridges():
    be = ScanBackend({2: (1, 10)})
    sup, clients, _ = make_supervisor(be)
    sup.initial_scan()
    sup.start(FakeControl())
    try:
        assert sup.check() is False
        assert clients[0].updates == [([2], set())]
    finally:
        sup.stop()


def test_supervisor_kick_runs_a_check_on_its_thread():
    be = ScanBackend({}, {3: (1, 11)})
    sup, clients, _ = make_supervisor(be)
    sup.initial_scan()
    sup.start(FakeControl())
    try:
        sup.kick()
        deadline = threading.Event()
        for _ in range(200):
            if clients:
                break
            deadline.wait(0.01)
        assert clients and clients[0].ran.wait(2)
    finally:
        sup.stop()


def test_supervisor_scan_error_keeps_state():
    be = ScanBackend({2: (1, 10)}, OSError("udevadm hung"))
    sup, clients, _ = make_supervisor(be)
    sup.initial_scan()
    sup.start(FakeControl())
    try:
        assert sup.check() is False
        assert not clients[0].stopped.is_set()
    finally:
        sup.stop()


def test_supervisor_falls_back_to_list_audio_slots():
    class OldBackend:
        def list_audio_slots(self):
            return [4]

    sup, _, _ = make_supervisor(OldBackend())
    assert sup.initial_scan() == [4]


# --- control plane hooks -----------------------------------------------------------------


def test_broker_set_virtual_modules_applies_on_next_inventory():
    from station_agent.broker import Broker

    class VM:
        slot = 1000
        module_id = "audio-router"

        def descriptor(self):
            return {"identity": {"type": "audio_router"}, "capabilities": []}

    async def send(_msg):
        pass

    broker = Broker(send)
    broker.set_inventory([])
    assert broker._descriptor(1000, "audio-router") is None
    broker.set_virtual_modules([VM()])
    broker.set_inventory([])
    assert broker._descriptor(1000, "audio-router") is not None
    broker.set_virtual_modules([])
    broker.set_inventory([])
    assert broker._descriptor(1000, "audio-router") is None


def _control_client(tmp_path, **kwargs):
    from station_agent.control_client import ControlClient
    from tests.test_control_client import _FakeConfig, _gen_key

    cfg = _FakeConfig("http://127.0.0.1:1", 1, _gen_key(tmp_path), str(tmp_path))
    cfg.control_rediscovery_interval = 0
    return ControlClient(cfg, **kwargs)


def test_control_rediscovery_cycle_fires_rescan_hook(tmp_path):
    from station_agent.control_client import _SlotInventoryDebouncer

    hits = []
    client = _control_client(tmp_path, on_rescan=lambda: hits.append(1))

    class FakeBroker:
        def __init__(self):
            self.scanned = asyncio.Event()

        async def rediscover(self, _fn):
            self.scanned.set()
            return []

        def set_inventory(self, _inv):
            pass

        async def emit_inventory(self):
            pass

    async def scenario():
        fake = FakeBroker()
        client._ws = object()
        client._refresh_event = asyncio.Event()
        client._loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            client._rediscovery_loop(fake, client._loop, _SlotInventoryDebouncer(), [])
        )
        client.request_inventory_refresh()
        await asyncio.wait_for(fake.scanned.wait(), timeout=2.0)
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert hits == [1]


def test_control_rescan_hook_error_does_not_break_loop(tmp_path):
    from station_agent.control_client import _SlotInventoryDebouncer

    def boom():
        raise RuntimeError("hook")

    client = _control_client(tmp_path, on_rescan=boom)

    class FakeBroker:
        def __init__(self):
            self.scans = 0
            self.second = asyncio.Event()

        async def rediscover(self, _fn):
            self.scans += 1
            if self.scans == 2:
                self.second.set()
            return []

        def set_inventory(self, _inv):
            pass

        async def emit_inventory(self):
            pass

    async def scenario():
        fake = FakeBroker()
        client._ws = object()
        client._refresh_event = asyncio.Event()
        client._loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            client._rediscovery_loop(fake, client._loop, _SlotInventoryDebouncer(), [])
        )
        client.request_inventory_refresh()
        await asyncio.sleep(0.05)
        client.request_inventory_refresh()
        await asyncio.wait_for(fake.second.wait(), timeout=2.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_control_set_virtual_modules_updates_live_broker_and_forces_emit(tmp_path):
    client = _control_client(tmp_path)
    calls = []

    class FakeBroker:
        def set_virtual_modules(self, vms):
            calls.append(("set", list(vms)))

    async def scenario():
        client._loop = asyncio.get_running_loop()
        client._broker = FakeBroker()
        client._refresh_event = asyncio.Event()
        vm = object()
        client.set_virtual_modules([vm])
        await asyncio.sleep(0.01)
        return vm

    vm = asyncio.run(scenario())
    assert calls == [("set", [vm])]
    assert client._virtual_modules == [vm]
    assert client._force_emit is True
    assert client._refresh_event.is_set()


def test_control_set_virtual_modules_while_disconnected_is_stored(tmp_path):
    client = _control_client(tmp_path)
    vm = object()
    client.set_virtual_modules([vm])
    assert client._virtual_modules == [vm]


# --- AudioClient.update_slots ------------------------------------------------------------


def _audio_client(tmp_path):
    from station_agent.audio.ws_client import AudioClient
    from tests.test_audio_ws_client import _FakeConfig, _gen_key

    return AudioClient(_FakeConfig("http://127.0.0.1:1", 1, _gen_key(tmp_path)))


class _RecEngine:
    def __init__(self):
        self.calls = []
        self.done = threading.Event()

    async def refresh(self, slots, rebuild=frozenset()):
        self.calls.append((slots, rebuild))
        self.done.set()


def test_audio_client_update_slots_refreshes_live_engine(tmp_path):
    client = _audio_client(tmp_path)
    engine = _RecEngine()

    async def scenario():
        client._loop = asyncio.get_running_loop()
        client._engine = engine
        await asyncio.to_thread(client.update_slots, [3], {3})
        for _ in range(100):
            if engine.done.is_set():
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert engine.calls == [([3], {3})]


def test_audio_client_update_slots_skips_stale_engine(tmp_path):
    client = _audio_client(tmp_path)
    stale, live = _RecEngine(), _RecEngine()

    async def scenario():
        client._engine = live  # reconnected before the scheduled refresh ran on the loop
        await client._refresh_engine(stale, [3], set())

    asyncio.run(scenario())
    assert stale.calls == [] and live.calls == []


def test_audio_client_update_slots_disconnected_is_noop(tmp_path):
    client = _audio_client(tmp_path)
    client.update_slots([3])  # no loop / engine: nothing scheduled, nothing raised
