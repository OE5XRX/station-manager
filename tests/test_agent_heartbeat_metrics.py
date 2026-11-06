"""Heartbeat wiring of the generic metrics block (spec §2.1, plan Task 8).

Pure agent-side tests: a fake HTTP client records the payloads, a fake collector
returns canned ``(values, descriptors)``. No Django, no network, no real sysfs.
"""

import threading

import pytest

from station_agent import heartbeat
from station_agent.heartbeat import send_heartbeat
from station_agent.metrics_contract import descriptors_hash

DESC_A = [
    {"key": "cpu.usage_pct", "label": "CPU usage", "unit": "%", "group": "system", "precision": 1}
]
DESC_B = DESC_A + [
    {
        "key": "pwr.12v.voltage_v",
        "label": "12 V rail voltage",
        "unit": "V",
        "group": "power",
        "precision": 2,
        "min": 0,
        "max": 30,
    }
]


class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = {"status": "ok"} if body is None else body
        self.text = str(self._body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Client:
    """Records payloads; returns queued responses (last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses) or [_Resp()]
        self.payloads: list[dict] = []

    def request(self, method, path, json_data=None):
        self.payloads.append(json_data)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class _Collector:
    def __init__(self, descriptors=DESC_A, values=None, raises=False):
        self.descriptors = descriptors
        self.values = {"cpu.usage_pct": 12.5} if values is None else values
        self.raises = raises
        self.rtts: list[float] = []

    def snapshot(self):
        if self.raises:
            raise RuntimeError("boom")
        return dict(self.values), [dict(d) for d in self.descriptors]

    def record_server_rtt(self, ms):
        self.rtts.append(ms)


@pytest.fixture(autouse=True)
def _cheap_system_info(monkeypatch):
    # Keep collect_system_info hermetic: no real inventory/telemetry/network probing.
    monkeypatch.setattr(heartbeat, "collect_inventory", lambda config=None: [])
    monkeypatch.setattr(heartbeat, "_collect_telemetry_safe", lambda config: {})
    monkeypatch.setattr(heartbeat, "get_ip_address", lambda: "10.0.0.1")


def test_first_heartbeat_includes_descriptors_and_hash():
    client, coll = _Client(), _Collector()
    assert send_heartbeat(client, metrics=coll) is True
    p = client.payloads[0]
    assert p["metrics"] == {"cpu.usage_pct": 12.5}
    assert p["metric_descriptors"] == DESC_A
    assert p["metric_descriptors_hash"] == descriptors_hash(DESC_A)


def test_unchanged_hash_omits_list_but_keeps_hash():
    client, coll = _Client(), _Collector()
    send_heartbeat(client, metrics=coll)
    send_heartbeat(client, metrics=coll)
    p = client.payloads[1]
    assert "metric_descriptors" not in p
    assert p["metric_descriptors_hash"] == descriptors_hash(DESC_A)
    assert p["metrics"] == {"cpu.usage_pct": 12.5}


def test_changed_hash_resends_list():
    client, coll = _Client(), _Collector()
    send_heartbeat(client, metrics=coll)
    coll.descriptors = DESC_B
    send_heartbeat(client, metrics=coll)
    p = client.payloads[1]
    assert p["metric_descriptors"] == DESC_B
    assert p["metric_descriptors_hash"] == descriptors_hash(DESC_B)
    send_heartbeat(client, metrics=coll)
    assert "metric_descriptors" not in client.payloads[2]


def test_server_request_forces_list_next_time():
    client = _Client(
        _Resp(), _Resp(body={"status": "ok", "metric_descriptors_required": True}), _Resp()
    )
    coll = _Collector()
    send_heartbeat(client, metrics=coll)  # list sent, accepted
    send_heartbeat(client, metrics=coll)  # no list; server asks for it
    assert "metric_descriptors" not in client.payloads[1]
    send_heartbeat(client, metrics=coll)
    assert client.payloads[2]["metric_descriptors"] == DESC_A
    send_heartbeat(client, metrics=coll)  # delivered with 200 → back to hash only
    assert "metric_descriptors" not in client.payloads[3]


def test_required_flag_must_be_literal_true():
    client = _Client(_Resp(), _Resp(body={"metric_descriptors_required": "yes"}))
    coll = _Collector()
    send_heartbeat(client, metrics=coll)
    send_heartbeat(client, metrics=coll)
    send_heartbeat(client, metrics=coll)
    assert "metric_descriptors" not in client.payloads[2]


@pytest.mark.parametrize("failure", [None, _Resp(500, "err")])
def test_failed_post_resends_list(failure):
    client = _Client(failure, _Resp())
    coll = _Collector()
    assert send_heartbeat(client, metrics=coll) is False
    send_heartbeat(client, metrics=coll)
    assert client.payloads[1]["metric_descriptors"] == DESC_A
    send_heartbeat(client, metrics=coll)
    assert "metric_descriptors" not in client.payloads[2]


def test_failed_forced_resend_keeps_forcing():
    client = _Client(
        _Resp(), _Resp(body={"metric_descriptors_required": True}), _Resp(503, "x"), _Resp()
    )
    coll = _Collector()
    for _ in range(4):
        send_heartbeat(client, metrics=coll)
    assert "metric_descriptors" in client.payloads[2]  # forced, but POST failed
    assert "metric_descriptors" in client.payloads[3]  # so forced again


def test_unparseable_body_does_not_break_marking():
    client = _Client(_Resp(200, ValueError("no json")), _Resp())
    coll = _Collector()
    assert send_heartbeat(client, metrics=coll) is True
    send_heartbeat(client, metrics=coll)
    assert "metric_descriptors" not in client.payloads[1]


def test_rtt_recorded_on_any_response(monkeypatch):
    ticks = iter([10.0, 10.25, 20.0, 20.5])
    monkeypatch.setattr(heartbeat.time, "monotonic", lambda: next(ticks))
    client = _Client(_Resp(), _Resp(500, "err"))
    coll = _Collector()
    send_heartbeat(client, metrics=coll)
    send_heartbeat(client, metrics=coll)
    assert coll.rtts == [pytest.approx(250.0), pytest.approx(500.0)]


def test_no_rtt_without_response():
    coll = _Collector()
    send_heartbeat(_Client(None), metrics=coll)
    assert coll.rtts == []


def test_rtt_recording_failure_is_harmless():
    class _Bad(_Collector):
        def record_server_rtt(self, ms):
            raise RuntimeError("nope")

    assert send_heartbeat(_Client(), metrics=_Bad()) is True


def test_metrics_none_keeps_legacy_payload():
    client = _Client()
    assert send_heartbeat(client) is True
    p = client.payloads[0]
    for key in ("metrics", "metric_descriptors", "metric_descriptors_hash"):
        assert key not in p


def test_snapshot_raising_still_sends_heartbeat():
    client = _Client()
    assert send_heartbeat(client, metrics=_Collector(raises=True)) is True
    p = client.payloads[0]
    assert "hostname" in p
    for key in ("metrics", "metric_descriptors", "metric_descriptors_hash"):
        assert key not in p


def test_state_is_per_collector():
    client = _Client()
    a, b = _Collector(), _Collector()
    send_heartbeat(client, metrics=a)
    send_heartbeat(client, metrics=b)  # fresh collector = fresh agent start
    assert "metric_descriptors" in client.payloads[1]


def test_tx_settings_and_metrics_share_one_parse():
    from station_agent.audio.tx_settings import TxAudioSettings

    parses = []

    class _Counting(_Resp):
        def json(self):
            parses.append(1)
            return super().json()

    h = TxAudioSettings()
    body = {"tx_audio": {"ceiling_dbfs": -7.5}, "metric_descriptors_required": True}
    send_heartbeat(_Client(_Counting(body=body)), tx_settings=h, metrics=_Collector())
    assert len(parses) == 1
    assert h.ceiling_dbfs == -7.5


def test_concurrent_heartbeats_are_safe():
    client, coll = _Client(), _Collector()
    errors = []

    def worker():
        try:
            for _ in range(50):
                assert send_heartbeat(client, metrics=coll) is True
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(p["metric_descriptors_hash"] == descriptors_hash(DESC_A) for p in client.payloads)
    # After the dust settles the state converged: hash-only from here on.
    send_heartbeat(client, metrics=coll)
    assert "metric_descriptors" not in client.payloads[-1]


# --------------------------------------------------------------------- agent wiring


def test_agent_starts_collector_and_passes_it_to_heartbeats(monkeypatch, tmp_path):

    events = []

    class _FakeCollector:
        def __init__(self, *a, **kw):
            events.append("init")

        def start(self):
            events.append("start")

        def stop(self, timeout=5.0):
            events.append("stop")

    agent, seen = _run_agent_once(monkeypatch, tmp_path, _FakeCollector)
    assert events == ["init", "start", "stop"]
    assert isinstance(seen[0], _FakeCollector)


def test_agent_survives_collector_start_failure(monkeypatch, tmp_path):

    class _Broken:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise OSError("no /proc")

        def stop(self, timeout=5.0):
            pass

    _agent, seen = _run_agent_once(monkeypatch, tmp_path, _Broken)
    assert seen == [None]  # heartbeat still sent, just without metrics


def _run_agent_once(monkeypatch, tmp_path, collector_cls):
    from station_agent import agent as agent_mod
    from station_agent.config import AgentConfig

    cfg = AgentConfig(
        server_url="https://x",
        station_id=1,
        ed25519_key_path="/tmp/k",
        state_dir=str(tmp_path / "state"),
        heartbeat_interval=60,
        ota_check_interval=1000,
    )
    seen = []

    class _Ctl:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            pass

        def stop(self):
            pass

    agent = agent_mod.StationAgent()

    def fake_heartbeat(client, **kw):
        seen.append(kw.get("metrics"))
        agent._shutdown.set()

    monkeypatch.setattr(agent_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(agent_mod, "HttpClient", lambda config: object())
    monkeypatch.setattr(agent_mod.signal, "signal", lambda *a: None)
    monkeypatch.setattr(agent_mod, "send_heartbeat", fake_heartbeat)
    monkeypatch.setattr(agent_mod, "MetricsCollector", collector_cls)
    monkeypatch.setattr(agent_mod, "ModuleOta", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_recover_module_ota", lambda: None)
    monkeypatch.setattr(
        "station_agent.audio.router_backend.PipeWireRouterBackend.audio_slot_generations",
        lambda self: {},
    )
    monkeypatch.setattr("station_agent.control_client.ControlClient", _Ctl)
    monkeypatch.setattr("station_agent.bootinfo.mark_clean_shutdown", lambda d: None)
    agent.run()
    return agent, seen


def test_keepalive_heartbeat_carries_collector(monkeypatch):
    import time

    from station_agent import agent as agent_mod
    from station_agent.config import AgentConfig

    seen = []
    monkeypatch.setattr(agent_mod, "HttpClient", lambda config: object())
    monkeypatch.setattr(
        agent_mod, "send_heartbeat", lambda client, **kw: seen.append(kw.get("metrics"))
    )
    agent = agent_mod.StationAgent()
    agent._config = AgentConfig(heartbeat_interval=0.05)
    sentinel = object()
    agent._metrics = sentinel

    class Ota:
        def recover_on_startup(self):
            time.sleep(0.2)

    agent._module_ota = Ota()
    agent._recover_module_ota()
    assert seen and all(m is sentinel for m in seen)
