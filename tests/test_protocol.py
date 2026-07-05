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
    assert msg == {
        "v": 1,
        "type": "state",
        "slot": 1,
        "module": "fm",
        "values": {"frequency": 145.5},
        "ts": 1234.0,
    }


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


# Fix 4: envelope version must be checked.
def test_parse_message_rejects_missing_version():
    with pytest.raises(p.ProtocolError) as exc:
        p.parse_message(json.dumps({"type": "command"}))
    assert exc.value.code == p.VALIDATION_FAILED


def test_parse_message_rejects_wrong_version():
    with pytest.raises(p.ProtocolError) as exc:
        p.parse_message(json.dumps({"v": 2, "type": "command"}))
    assert exc.value.code == p.VALIDATION_FAILED
