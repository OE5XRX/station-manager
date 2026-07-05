"""Agent<->Server control-protocol envelope, message builders, and error codes.

Pure data helpers — no I/O. The envelope is ``{"v": PROTOCOL_VERSION, "type": ...}``
on every message (design spec §7). Protocol version ``v`` and descriptor ``schema``
version are independent (§10).
"""

from __future__ import annotations

import json

PROTOCOL_VERSION = 1

# Structured error codes (§10). FW MODULE-RESULT error strings pass through as-is.
VALIDATION_FAILED = "validation_failed"
UNKNOWN_SLOT = "unknown_slot"
UNKNOWN_MODULE = "unknown_module"
UNKNOWN_CAPABILITY = "unknown_capability"
WRONG_OP = "wrong_op"
READ_ONLY = "read_only"
OUT_OF_RANGE = "out_of_range"
BAD_VALUE = "bad_value"
TIMEOUT = "timeout"


class ProtocolError(Exception):
    """A structured protocol/validation error carrying a code + human message."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code
        self.msg = msg


def _envelope(msg_type: str, **fields) -> dict:
    return {"v": PROTOCOL_VERSION, "type": msg_type, **fields}


def build_result(request_id, ok: bool, value=None, error=None) -> dict:
    """Build a ``result`` reply. On error, ``error`` is a (code, msg) tuple."""
    msg = _envelope("result", request_id=request_id, ok=bool(ok))
    if ok:
        msg["value"] = value
    else:
        code, emsg = error if error is not None else (VALIDATION_FAILED, "")
        msg["error"] = {"code": code, "msg": emsg}
    return msg


def build_state(slot, module, values: dict, ts: float) -> dict:
    return _envelope("state", slot=slot, module=module, values=values, ts=ts)


def build_event(slot, module, event: str, detail) -> dict:
    return _envelope("event", slot=slot, module=module, event=event, detail=detail)


def build_inventory(slots: list) -> dict:
    return _envelope("inventory", slots=slots)


def parse_message(text: str) -> dict:
    """Parse an inbound frame; raise ProtocolError(VALIDATION_FAILED) if malformed."""
    try:
        msg = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise ProtocolError(VALIDATION_FAILED, "message is not valid JSON")
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError(VALIDATION_FAILED, "message missing 'type'")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(VALIDATION_FAILED, "unsupported protocol version")
    return msg
