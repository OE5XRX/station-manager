import json
import threading

import pytest
from django.core.cache import cache
from django.urls import reverse

from station_agent.audio import tx_settings as ts
from station_agent.heartbeat import send_heartbeat
from tests.conftest import device_auth_headers


def test_holder_defaults_safe():
    assert ts.TxAudioSettings().ceiling_dbfs == ts.CEILING_DEFAULT_DBFS


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"status": "ok", "tx_audio": {"ceiling_dbfs": -8.0}}, -8.0),
        ({"status": "ok", "tx_audio": {"ceiling_dbfs": 3.0}}, -3.0),
        ({"status": "ok", "tx_audio": {"ceiling_dbfs": "x"}}, -12.0),
        ({"status": "ok", "tx_audio": {"ceiling_dbfs": float("nan")}}, -12.0),
        ({"status": "ok", "tx_audio": None}, -12.0),
        ({"status": "ok"}, -12.0),  # old server: no key -> safe default
        (None, -12.0),
        ("garbage", -12.0),
        ({"tx_audio": {"ceiling_dbfs": 10**400}}, -12.0),
        ({"tx_audio": {"ceiling_dbfs": -(10**400)}}, -12.0),
        ({"tx_audio": {"ceiling_dbfs": True}}, -12.0),
        ({"tx_audio": {"ceiling_dbfs": float("inf")}}, -12.0),
        ({"tx_audio": {"ceiling_dbfs": -40}}, -24.0),
        ({"tx_audio": [1, 2]}, -12.0),
        ([1, 2], -12.0),
        (5, -12.0),
    ],
)
def test_update_from_heartbeat_is_total(body, expected):
    h = ts.TxAudioSettings()
    h.update_from_heartbeat(body)
    assert h.ceiling_dbfs == expected


def test_overflow_never_louder_than_default():
    # Unusable value → min(current, default): a louder -6 falls back to the -12 default.
    h = ts.TxAudioSettings()
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -6.0}})
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": 10**400}})
    assert h.ceiling_dbfs == -12.0


def test_overflow_keeps_quieter_calibration():
    # Spec §4 fail-safe toward under-deviation: a calibrated -20 must NOT be raised to -12
    # just because one heartbeat body was unusable.
    h = ts.TxAudioSettings()
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -20.0}})
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": 10**400}})
    assert h.ceiling_dbfs == -20.0


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "garbage",
        5,
        [1, 2],
        {"status": "ok"},
        {"tx_audio": None},
        {"tx_audio": [1, 2]},
        {"tx_audio": {"ceiling_dbfs": "x"}},
        {"tx_audio": {"ceiling_dbfs": True}},
        {"tx_audio": {"ceiling_dbfs": float("nan")}},
        {"tx_audio": {"ceiling_dbfs": float("inf")}},
        {"tx_audio": {"ceiling_dbfs": -(10**400)}},
    ],
)
def test_unusable_body_never_raises_a_quieter_ceiling(bad):
    h = ts.TxAudioSettings()
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -20.0}})
    h.update_from_heartbeat(bad)
    assert h.ceiling_dbfs == -20.0


def test_valid_server_value_is_authoritative_both_directions():
    h = ts.TxAudioSettings()
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -20.0}})
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -6.0}})
    assert h.ceiling_dbfs == -6.0  # louder valid value applied as-is
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -18.0}})
    assert h.ceiling_dbfs == -18.0  # quieter valid value applied as-is


def test_old_value_falls_back_to_default_when_louder_and_server_drops_it():
    h = ts.TxAudioSettings()
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -6.0}})
    h.update_from_heartbeat({"status": "ok"})
    assert h.ceiling_dbfs == -12.0


def test_thread_safe_reads():
    h = ts.TxAudioSettings()
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -6.0}})
            h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -9.0}})

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(2000):
            assert h.ceiling_dbfs in (-6.0, -9.0, -12.0)
    finally:
        stop.set()
        t.join()


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Client:
    def __init__(self, resp):
        self.resp = resp

    def request(self, *a, **k):
        return self.resp


def test_send_heartbeat_feeds_holder(monkeypatch):
    monkeypatch.setattr("station_agent.heartbeat.collect_system_info", lambda config=None: {})
    h = ts.TxAudioSettings()
    ok = send_heartbeat(_Client(_Resp(200, {"tx_audio": {"ceiling_dbfs": -7.5}})), tx_settings=h)
    assert ok is True
    assert h.ceiling_dbfs == -7.5


def test_send_heartbeat_survives_non_json_body(monkeypatch):
    monkeypatch.setattr("station_agent.heartbeat.collect_system_info", lambda config=None: {})
    h = ts.TxAudioSettings()
    assert send_heartbeat(_Client(_Resp(200, ValueError("no json"))), tx_settings=h) is True
    assert h.ceiling_dbfs == -12.0


def test_send_heartbeat_survives_recursion_error(monkeypatch):
    monkeypatch.setattr("station_agent.heartbeat.collect_system_info", lambda config=None: {})
    h = ts.TxAudioSettings()
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -6.0}})
    assert send_heartbeat(_Client(_Resp(200, RecursionError("deep"))), tx_settings=h) is True
    assert h.ceiling_dbfs == -12.0  # -6 is louder than the default → min(-6, -12)


def test_send_heartbeat_unparseable_body_keeps_quieter_calibration(monkeypatch):
    monkeypatch.setattr("station_agent.heartbeat.collect_system_info", lambda config=None: {})
    h = ts.TxAudioSettings()
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -20.0}})
    assert send_heartbeat(_Client(_Resp(200, RecursionError("deep"))), tx_settings=h) is True
    assert h.ceiling_dbfs == -20.0


def test_agent_and_server_clamps_agree():
    from apps.stations import tx_audio

    for v in (None, -40, -24, -12.3, -3, 0, float("nan"), "x", True, 10**400, -(10**400)):
        assert ts.clamp_ceiling(v) == tx_audio.effective_ceiling_dbfs(v)


@pytest.mark.django_db
class TestHeartbeatResponseTxAudio:
    @pytest.fixture(autouse=True)
    def _clear_throttle_bucket(self):
        cache.clear()

    def _post(self, client, station, private_key):
        payload = {
            "hostname": "station-01",
            "os_version": "Yocto 4.0",
            "uptime": 3600.0,
            "ip_address": "192.168.1.100",
            "module_versions": {},
        }
        body = json.dumps(payload).encode("utf-8")
        return client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )

    def test_heartbeat_response_carries_effective_ceiling(self, client, station_with_key):
        station, private_key = station_with_key
        station.tx_audio_ceiling_dbfs = -9.0
        station.save(update_fields=["tx_audio_ceiling_dbfs"])
        resp = self._post(client, station, private_key)
        assert resp.status_code == 200
        assert resp.json()["tx_audio"] == {"ceiling_dbfs": -9.0, "calibrated": True}

    def test_heartbeat_response_uncalibrated_default(self, client, station_with_key):
        station, private_key = station_with_key
        station.tx_audio_ceiling_dbfs = None
        station.save(update_fields=["tx_audio_ceiling_dbfs"])
        resp = self._post(client, station, private_key)
        assert resp.status_code == 200
        assert resp.json()["tx_audio"] == {"ceiling_dbfs": -12.0, "calibrated": False}
