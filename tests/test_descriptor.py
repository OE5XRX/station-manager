# tests/test_descriptor.py
import pytest

from station_agent import descriptor as d
from station_agent import protocol as p

FM = {
    "schema": 1,
    "module": "fm",
    "capabilities": [
        {
            "name": "frequency",
            "kind": "setting",
            "type": "float",
            "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}],
        },
        {"name": "volume", "kind": "setting", "type": "int", "ranges": [{"min": 1, "max": 8}]},
        {"name": "power_level", "kind": "setting", "type": "enum", "values": ["low", "high"]},
        {"name": "ptt", "kind": "action", "type": "bool"},
        {
            "name": "rssi",
            "kind": "telemetry",
            "type": "int",
            "readonly": True,
            "min_interval_ms": 250,
        },
        {"name": "band", "kind": "telemetry", "type": "string", "readonly": True},
    ],
}


def caps():
    return d.index_capabilities(FM)


def test_index_capabilities_by_name():
    idx = caps()
    assert set(idx) == {"frequency", "volume", "power_level", "ptt", "rssi", "band"}
    assert idx["frequency"]["type"] == "float"


def test_unknown_capability_rejected():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(None, "set", 145.5)
    assert e.value.code == p.UNKNOWN_CAPABILITY


def test_setting_accepts_set_and_get():
    d.validate_command(caps()["frequency"], "set", 145.5)  # no raise
    d.validate_command(caps()["frequency"], "get", None)  # no raise


def test_setting_rejects_do_as_wrong_op():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["frequency"], "do", 145.5)
    assert e.value.code == p.WRONG_OP


def test_action_accepts_do_and_get_but_not_set():
    d.validate_command(caps()["ptt"], "do", True)
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["ptt"], "set", True)
    assert e.value.code == p.WRONG_OP


def test_telemetry_get_ok_set_is_read_only():
    d.validate_command(caps()["rssi"], "get", None)
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["rssi"], "set", 5)
    assert e.value.code == p.READ_ONLY


def test_float_out_of_range_rejected_before_fw():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["frequency"], "set", 200.0)
    assert e.value.code == p.OUT_OF_RANGE


def test_int_out_of_range_rejected():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["volume"], "set", 9)
    assert e.value.code == p.OUT_OF_RANGE


def test_enum_not_in_values_rejected():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["power_level"], "set", "medium")
    assert e.value.code == p.BAD_VALUE


def test_bool_type_mismatch_rejected():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["ptt"], "do", "on")  # string, not bool
    assert e.value.code == p.BAD_VALUE


def test_int_rejects_bool_value():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["volume"], "set", True)
    assert e.value.code == p.BAD_VALUE


def test_set_requires_value():
    with pytest.raises(p.ProtocolError) as e:
        d.validate_command(caps()["frequency"], "set", None)
    assert e.value.code == p.BAD_VALUE


def test_format_value_canonical_tokens():
    assert d.format_value("bool", True) == "true"
    assert d.format_value("bool", False) == "false"
    assert d.format_value("int", 7) == "7"
    assert d.format_value("float", 145.5) == "145.5"
    assert d.format_value("float", 146.0) == "146.0"
    assert d.format_value("enum", "high") == "high"
    assert d.format_value("string", "67.0") == "67.0"


def test_min_interval_from_descriptor_else_default():
    assert d.min_interval_ms(caps()["rssi"], default=100) == 250
    assert d.min_interval_ms(caps()["band"], default=100) == 100


# Fix 1: get with non-None value must be rejected.
def test_get_with_value_raises_bad_value():
    with pytest.raises(p.ProtocolError) as exc:
        d.validate_command(caps()["rssi"], "get", 5)
    assert exc.value.code == p.BAD_VALUE


# Fix 2: format_value float must never produce scientific notation.
def test_format_value_float_no_exponent():
    result = d.format_value("float", 1e-06)
    assert "e" not in result and "E" not in result
    assert float(result) == 1e-06


def test_string_value_token_charset_enforced():
    """String values must match the canonical FW token charset (TOKEN_RE)."""
    cap = {"name": "label", "kind": "setting", "type": "string"}
    # Valid token must pass.
    d.validate_command(cap, "set", "ok")
    # String with a space must be rejected.
    with pytest.raises(p.ProtocolError) as exc:
        d.validate_command(cap, "set", "a b")
    assert exc.value.code == p.BAD_VALUE
    # Empty string must be rejected.
    with pytest.raises(p.ProtocolError) as exc:
        d.validate_command(cap, "set", "")
    assert exc.value.code == p.BAD_VALUE
    # String with '@' (outside charset) must also be rejected.
    with pytest.raises(p.ProtocolError) as exc:
        d.validate_command(cap, "set", "a@b")
    assert exc.value.code == p.BAD_VALUE


def test_format_value_float_tiny_no_precision_loss():
    # A magnitude that repr() renders in scientific notation must become a
    # lossless fixed-point token — not rounded to 0.0 by a ".10f" format.
    out = d.format_value("float", 1e-12)
    assert "e" not in out and "E" not in out
    assert float(out) == 1e-12
