import asyncio

from station_agent.broker import Broker
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

_F = {"name": "f", "kind": "setting", "type": "float", "ranges": [{"min": 100.0, "max": 500.0}]}
FM = {
    "schema": 1,
    "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {**_F, "name": "tx_frequency"},
        {**_F, "name": "rx_frequency"},
        {"name": "tx_tone", "kind": "setting", "type": "string"},
        {"name": "rx_tone", "kind": "setting", "type": "string"},
        {"name": "bandwidth", "kind": "setting", "type": "string"},
        {"name": "ptt", "kind": "action", "type": "bool"},
    ],
}


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def _setup():
    fw = FakeFirmware({"fm": FM})
    fw.start()
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


def _frame(values):
    return {
        "v": 1,
        "type": "apply_preset",
        "request_id": "p1",
        "slot": 1,
        "module": "fm",
        "values": values,
    }


FULL = {
    "bandwidth": "25",
    "rx_tone": "none",
    "tx_tone": "77.0",
    "rx_frequency": 145.6,
    "tx_frequency": 145.0,
}


def test_applies_in_order():
    fw, b, col = _setup()
    try:
        asyncio.run(b.handle(_frame(FULL)))
        sets = [ln.split()[3] for ln in map(bytes.decode, fw.received) if " set " in ln]
        assert sets == ["tx_frequency", "rx_frequency", "tx_tone", "rx_tone", "bandwidth"]
        res = col.sent[0]
        assert res["type"] == "result" and res["ok"] is True
        assert all(v["ok"] for v in res["value"].values())
    finally:
        fw.stop()


def test_partial_failure_reported_per_cap():
    fw, b, col = _setup()
    try:
        asyncio.run(b.handle(_frame({"tx_frequency": 145.0, "rx_frequency": 999.0})))
        res = col.sent[0]
        assert res["ok"] is False
        assert res["value"]["tx_frequency"] == {"ok": True}
        assert res["value"]["rx_frequency"]["ok"] is False
        assert res["value"]["rx_frequency"]["error"]["code"]
    finally:
        fw.stop()


def test_refused_while_ptt():
    fw, b, col = _setup()
    try:
        b._ptt_inflight.add((1, "fm"))
        asyncio.run(b.handle(_frame(FULL)))
        assert col.sent[0]["ok"] is False
        assert col.sent[0]["error"]["code"] == "ptt_active"
        assert not any(b" set " in ln for ln in fw.received)
    finally:
        fw.stop()


def test_state_frame_contains_only_ok_caps():
    fw, b, col = _setup()
    try:
        asyncio.run(b.handle(_frame({"tx_frequency": 145.0, "rx_frequency": 999.0})))
        states = [m for m in col.sent if m["type"] == "state"]
        assert len(states) == 1
        assert list(states[0]["values"]) == ["tx_frequency"]
    finally:
        fw.stop()


def test_public_apply_preset_returns_per_cap_dict():
    fw, b, _ = _setup()
    try:
        out = asyncio.run(b.apply_preset(1, "fm", {"tx_frequency": 145.0}))
        assert out["tx_frequency"]["ok"] is True
    finally:
        fw.stop()
