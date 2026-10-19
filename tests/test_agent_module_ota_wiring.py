"""Task A6: module_ota wired into the StationAgent main loop.

``StationAgent.run()`` runs in a background thread with every collaborator faked
(config, HTTP client, signal registration, heartbeat, control client, audio detection,
image OTA, ``ModuleOta``). The tests drive the loop through the control client's
``on_firmware_check`` nudge and ``_handle_signal``.
"""

from __future__ import annotations

import contextlib
import signal
import threading
import time

import pytest

from station_agent import agent as agent_mod
from station_agent.agent import StationAgent
from station_agent.config import AgentConfig

WAIT = 3.0


def _until(pred, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class FakeControlClient:
    instances: list[FakeControlClient] = []

    def __init__(self, config, **kwargs):
        self.kwargs = kwargs
        self.stopped = threading.Event()
        FakeControlClient.instances.append(self)

    def run(self):
        self.stopped.wait()

    def stop(self):
        self.stopped.set()


class FakeModuleOta:
    def __init__(self, calls, raises=False, ctor_raises=False, block=0.0):
        self.calls = calls
        self.raises = raises
        self.ctor_raises = ctor_raises
        self.block = block
        self.created = threading.Event()
        self.init_args = None
        self.run_times: list[float] = []

    def __call__(self, config, http_client, **kwargs):  # acts as the ModuleOta class
        self.init_args = (config, http_client, kwargs)
        self.created.set()
        if self.ctor_raises:
            raise RuntimeError("ctor boom")
        return self

    def advertised_backends(self):
        return ["dfu"]

    def recover_on_startup(self):
        self.calls.append("recover")
        if self.raises:
            raise RuntimeError("recover boom")

    def run_once(self):
        self.calls.append("module_ota")
        self.run_times.append(time.monotonic())
        if self.block:
            time.sleep(self.block)
            self.calls.append("module_ota_done")
        if self.raises:
            raise RuntimeError("run_once boom")


HB_KWARGS: list[dict] = []


@pytest.fixture
def harness(monkeypatch, tmp_path):
    FakeControlClient.instances = []
    hb_kwargs = HB_KWARGS
    started = []

    def start(
        heartbeat_interval=3600,
        ota_check_interval=1000,
        raises=False,
        ctor_raises=False,
        block=0.0,
        image_ota_shuts_down=False,
    ):
        calls: list[str] = []
        clients: list[object] = []
        hb_kwargs.clear()
        cfg = AgentConfig(
            server_url="https://x",
            station_id=1,
            ed25519_key_path="/tmp/k",
            state_dir=str(tmp_path / "state"),
            heartbeat_interval=heartbeat_interval,
            ota_check_interval=ota_check_interval,
        )
        fake = FakeModuleOta(calls, raises=raises, ctor_raises=ctor_raises, block=block)
        monkeypatch.setattr(agent_mod, "load_config", lambda: cfg)
        monkeypatch.setattr(
            agent_mod, "HttpClient", lambda config: clients.append(object()) or clients[-1]
        )
        monkeypatch.setattr(agent_mod.signal, "signal", lambda *a: None)

        def fake_heartbeat(client, **kw):
            # The main loop uses the first client; the keepalive must bring its own.
            calls.append("heartbeat" if client is clients[0] else "keepalive")
            hb_kwargs.append(kw)

        monkeypatch.setattr(agent_mod, "send_heartbeat", fake_heartbeat)
        monkeypatch.setattr(agent_mod, "ModuleOta", fake)
        monkeypatch.setattr("station_agent.audio.detect.audio_path_present", lambda c: False)
        monkeypatch.setattr("station_agent.control_client.ControlClient", FakeControlClient)
        agent = StationAgent()

        def fake_image_ota(c, h):
            calls.append("image_ota")
            if image_ota_shuts_down:
                agent._shutdown.set()  # SIGTERM from the reboot the image OTA queued

        monkeypatch.setattr(agent, "_handle_ota", fake_image_ota)
        thread = threading.Thread(target=agent.run, daemon=True)
        thread.start()
        started.append((agent, thread))
        assert fake.created.wait(WAIT), "ModuleOta was never constructed"
        assert _until(lambda: "heartbeat" in calls)
        return agent, thread, fake, calls

    yield start
    for agent, thread in started:
        agent._shutdown.set()
        agent._wake.set()
        thread.join(WAIT)


def _nudge():
    return FakeControlClient.instances[-1].kwargs["on_firmware_check"]


def test_module_ota_built_with_control_and_audio(harness):
    agent, _, fake, _ = harness()
    _cfg, _http, kwargs = fake.init_args
    assert kwargs == {"control": FakeControlClient.instances[-1], "audio": None}
    assert _nudge() == agent._wake.set


def test_recover_on_startup_called_once(harness):
    _, _, fake, calls = harness()
    _nudge()()
    assert _until(lambda: "module_ota" in calls)
    assert calls.count("recover") == 1
    assert calls.index("recover") < calls.index("heartbeat")


def test_nudge_triggers_module_ota_run(harness):
    _, _, _, calls = harness()
    assert "module_ota" not in calls
    _nudge()()
    assert _until(lambda: "module_ota" in calls)
    # The nudge does not cost an extra heartbeat.
    time.sleep(0.1)
    assert calls.count("heartbeat") == 1


class _ScriptedClock:
    """Fake monotonic clock + ``_wake`` event: ``wait`` advances time instead of sleeping."""

    def __init__(self, nudges):
        self.now = 1000.0
        self.nudges = sorted(nudges)  # absolute times at which a nudge arrives
        self.flag = False

    def __call__(self):
        return self.now

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, timeout):
        assert timeout > 0, "busy loop: non-positive wait timeout"
        if self.nudges and self.nudges[0] <= self.now + timeout:
            self.now = max(self.now, self.nudges.pop(0))
            self.flag = True
            return True
        self.now += timeout
        return self.flag


def test_nudge_rate_limited(monkeypatch):
    """Deterministic: injected clock, nudges at t=0, 2, 3 (s after start), interval 10 s."""
    clock = _ScriptedClock([1000.0, 1002.0, 1003.0])
    agent = StationAgent()
    agent._clock = clock
    agent._wake = clock
    runs: list[float] = []

    class Ota:
        def run_once(self):
            runs.append(clock.now)

    agent._module_ota = Ota()
    monkeypatch.setattr(agent, "_heartbeat_keepalive", contextlib.nullcontext)
    agent._wait_for_next_heartbeat(60)
    # First nudge runs at once; the two inside the window coalesce into one run at t=10.
    assert runs == [1000.0, 1000.0 + agent_mod.NUDGE_MIN_INTERVAL_S]
    assert clock.now == 1060.0  # the heartbeat deadline is kept; no extra heartbeat


def test_deferred_nudge_carries_over_heartbeat_deadline(monkeypatch):
    clock = _ScriptedClock([1000.0, 1005.0])
    agent = StationAgent()
    agent._clock = clock
    agent._wake = clock
    runs: list[float] = []

    class Ota:
        def run_once(self):
            runs.append(clock.now)

    agent._module_ota = Ota()
    monkeypatch.setattr(agent, "_heartbeat_keepalive", contextlib.nullcontext)
    agent._wait_for_next_heartbeat(8)  # deadline t=8 < t=10 window end
    assert runs == [1000.0]
    agent._wait_for_next_heartbeat(8)
    assert runs == [1000.0, 1010.0]


def test_shutdown_wakes_loop_and_exits(harness):
    agent, thread, _, calls = harness(heartbeat_interval=3600)
    agent._handle_signal(signal.SIGTERM, None)
    assert agent._wake.is_set()
    thread.join(WAIT)
    assert not thread.is_alive()
    assert "module_ota" not in calls


def test_cadence_runs_module_ota_after_image_ota(harness):
    _, _, _, calls = harness(heartbeat_interval=0.05, ota_check_interval=2)
    assert _until(lambda: calls.count("module_ota") >= 2)
    seq = [c for c in calls if c != "recover"]
    # heartbeat, heartbeat, image_ota, module_ota, heartbeat, heartbeat, image_ota, module_ota
    first = seq.index("module_ota")
    assert seq[first - 1] == "image_ota"
    assert seq[:first].count("heartbeat") == 2


def test_module_ota_exception_does_not_kill_loop(harness, monkeypatch):
    monkeypatch.setattr(agent_mod, "NUDGE_MIN_INTERVAL_S", 0.0)
    _, thread, _, calls = harness(heartbeat_interval=0.05, ota_check_interval=1, raises=True)
    assert calls[0] == "recover"  # raised, loop started anyway
    assert _until(lambda: calls.count("module_ota") >= 2)  # cadence runs raised
    _nudge()()
    n = calls.count("module_ota")
    assert _until(lambda: calls.count("module_ota") > n)
    assert _until(lambda: calls.count("heartbeat") >= 4)
    assert thread.is_alive()


def test_module_ota_construction_failure_keeps_agent_running(harness):
    _, thread, _, calls = harness(heartbeat_interval=0.05, ota_check_interval=1, ctor_raises=True)
    _nudge()()
    assert _until(lambda: calls.count("heartbeat") >= 3)
    assert thread.is_alive()
    assert "module_ota" not in calls and "recover" not in calls


def test_heartbeat_advertises_module_ota_backends(harness):
    """Capabilities come from the running ModuleOta, never a separate detection."""
    harness(heartbeat_interval=0.05)
    assert _until(lambda: len(HB_KWARGS) >= 2)
    assert all(kw.get("module_ota_backends") == ["dfu"] for kw in HB_KWARGS[1:])


def test_heartbeat_advertises_no_backends_without_module_ota(harness):
    harness(heartbeat_interval=0.05, ctor_raises=True)
    assert _until(lambda: len(HB_KWARGS) >= 2)
    assert all(kw.get("module_ota_backends") == [] for kw in HB_KWARGS)


def test_no_module_ota_after_shutdown_from_image_ota(harness):
    """Image OTA queued a reboot and SIGTERM arrived: module OTA must not start a flash."""
    _, thread, _, calls = harness(
        heartbeat_interval=0.05, ota_check_interval=1, image_ota_shuts_down=True
    )
    thread.join(WAIT)
    assert not thread.is_alive()
    assert "image_ota" in calls
    assert "module_ota" not in calls


def test_no_module_ota_when_shutting_down():
    agent = StationAgent()
    calls = []

    class Ota:
        def run_once(self):
            calls.append("run")

        def recover_on_startup(self):
            calls.append("recover")

    agent._module_ota = Ota()
    agent._shutdown.set()
    agent._run_module_ota()
    agent._recover_module_ota()
    assert calls == []


def test_keepalive_heartbeats_during_long_module_ota(harness):
    _, thread, _, calls = harness(heartbeat_interval=0.05, block=0.4)
    _nudge()()
    assert _until(lambda: "module_ota_done" in calls)
    start = calls.index("module_ota")
    done = calls.index("module_ota_done")
    during = calls[start:done]
    assert during.count("keepalive") >= 2
    assert "heartbeat" not in during  # main loop blocked: only the keepalive heartbeats
    n = calls.count("keepalive")
    time.sleep(0.3)
    assert calls.count("keepalive") == n  # stopped once run_once returned
    assert thread.is_alive()


def test_keepalive_during_startup_recovery(monkeypatch):
    sent = []
    clients = []
    monkeypatch.setattr(
        agent_mod, "HttpClient", lambda config: clients.append(object()) or clients[-1]
    )
    monkeypatch.setattr(agent_mod, "send_heartbeat", lambda client, **kw: sent.append(client))
    agent = StationAgent()
    agent._config = AgentConfig(heartbeat_interval=0.05)

    class Ota:
        def recover_on_startup(self):
            time.sleep(0.3)

    agent._module_ota = Ota()
    agent._recover_module_ota()
    assert len(sent) >= 2
    assert set(map(id, sent)) == {id(clients[0])}  # its own client
    n = len(sent)
    time.sleep(0.15)
    assert len(sent) == n


def test_keepalive_not_started_when_shutting_down(monkeypatch):
    started = []
    monkeypatch.setattr(agent_mod, "HttpClient", lambda config: started.append(1))
    agent = StationAgent()
    agent._config = AgentConfig(heartbeat_interval=0.01)
    agent._shutdown.set()
    with agent._heartbeat_keepalive():
        time.sleep(0.05)
    assert started == []
    assert not [t for t in threading.enumerate() if t.name == "module-ota-keepalive"]
