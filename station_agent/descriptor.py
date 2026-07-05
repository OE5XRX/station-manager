"""Descriptor-driven command validation + value formatting.

Pure, no I/O. The broker validates every command against the cached ``describe``
descriptor *before* the firmware (spec §12): existence, kind<->op gating, type,
range, and enum are all checked here. Value formatting turns a validated JSON value
into the canonical firmware token. Adding a module / capability / value type needs
NO change here — it is all read from the descriptor.
"""

from __future__ import annotations

from decimal import Decimal

from station_agent.protocol import (
    BAD_VALUE,
    OUT_OF_RANGE,
    READ_ONLY,
    UNKNOWN_CAPABILITY,
    WRONG_OP,
    ProtocolError,
)

# kind -> the set of ops that kind accepts (mirrors FW iface.h mixins).
_OPS_FOR_KIND = {
    "setting": {"set", "get"},
    "action": {"do", "get"},
    "telemetry": {"get"},
}
_WRITE_OPS = {"set", "do"}


def index_capabilities(module_descriptor: dict) -> dict:
    """Return {capability_name: descriptor_dict} for a module's ``describe`` output."""
    out = {}
    for cap in module_descriptor.get("capabilities", []):
        name = cap.get("name")
        if isinstance(name, str):
            out[name] = cap
    return out


def validate_command(cap: dict | None, op: str, value) -> None:
    """Raise ProtocolError if (cap, op, value) is invalid. Return None if valid."""
    if cap is None:
        raise ProtocolError(UNKNOWN_CAPABILITY, "no such capability")

    kind = cap.get("kind", "")
    allowed = _OPS_FOR_KIND.get(kind, set())

    # A write to telemetry or a readonly cap is read_only; other bad ops are wrong_op.
    if op in _WRITE_OPS and (kind == "telemetry" or cap.get("readonly")):
        raise ProtocolError(READ_ONLY, f"{cap.get('name')} is read-only")
    if op not in allowed:
        raise ProtocolError(WRONG_OP, f"op {op!r} not valid for kind {kind!r}")

    if op == "get":
        if value is not None:
            raise ProtocolError(BAD_VALUE, "get takes no value")
        return

    if value is None:
        raise ProtocolError(BAD_VALUE, "value required")

    _check_value(cap, value)


def _check_value(cap: dict, value) -> None:
    vtype = cap.get("type")
    if vtype == "bool":
        if not isinstance(value, bool):
            raise ProtocolError(BAD_VALUE, "expected bool")
        return
    if vtype == "enum":
        if value not in cap.get("values", []):
            raise ProtocolError(BAD_VALUE, f"{value!r} not an enum value")
        return
    if vtype == "string":
        if not isinstance(value, str):
            raise ProtocolError(BAD_VALUE, "expected string")
        if not value or any(c.isspace() for c in value):
            raise ProtocolError(BAD_VALUE, "string value must be non-empty and whitespace-free")
        return
    if vtype == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProtocolError(BAD_VALUE, "expected int")
        _check_range(cap, value)
        return
    if vtype == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError(BAD_VALUE, "expected number")
        _check_range(cap, float(value))
        return
    raise ProtocolError(BAD_VALUE, f"unknown value type {vtype!r}")


def _check_range(cap: dict, value) -> None:
    ranges = cap.get("ranges")
    if not ranges:
        return
    for r in ranges:
        if r.get("min", float("-inf")) <= value <= r.get("max", float("inf")):
            return
    raise ProtocolError(OUT_OF_RANGE, f"{value} outside all ranges")


def format_value(cap_type: str, value) -> str:
    """Canonical firmware token for a validated value."""
    if cap_type == "bool":
        return "true" if value else "false"
    if cap_type == "int":
        return str(int(value))
    if cap_type == "float":
        # Always-decimal, no exponent; matches the FW's whole-string float parse.
        # repr() may produce scientific notation for very small/large values; reformat
        # those without exponent so the FW token parser always sees a plain decimal.
        text = repr(float(value))
        if "e" in text or "E" in text:
            # repr() used scientific notation; expand to a lossless fixed-point
            # string via Decimal. A rounding format like ".10f" would flush tiny
            # magnitudes (e.g. 1e-12 -> 0.0) and send the wrong token to the FW.
            text = format(Decimal(text), "f")
        return text
    # enum / string are passed through verbatim.
    return str(value)


def min_interval_ms(cap: dict, default: int) -> int:
    """Descriptor-declared min_interval_ms if present & positive, else the default."""
    val = cap.get("min_interval_ms")
    if isinstance(val, int) and not isinstance(val, bool) and val > 0:
        return val
    return default
