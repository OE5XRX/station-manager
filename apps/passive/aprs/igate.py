"""APRS-IS gating rules, passcode and beacon: pure functions, no I/O.

RX-only iGate (RF -> IS): the path gets ``qAR,<igate>`` appended; packets that already
travelled over the internet or ask not to be gated are skipped with a reason."""

import re
from typing import TYPE_CHECKING

from .parse import parse_thirdparty, valid_header

if TYPE_CHECKING:
    from ..models import AprsPacket

NOGATE_TOKENS = ("TCPIP", "TCPXX", "NOGATE", "RFONLY")
TOCALL = "APZXRX"


def passcode(callsign: str) -> int:
    """The standard APRS-IS hash over the upper-case call without SSID."""
    call = callsign.upper().split("-", 1)[0]
    h = 0x73E2
    for i in range(0, len(call), 2):
        h ^= ord(call[i]) << 8
        if i + 1 < len(call):
            h ^= ord(call[i + 1])
    return h & 0x7FFF


_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_CALL_SSID = re.compile(r"[A-Za-z0-9]{1,9}(-[0-9]{1,2})?", re.ASCII)


def _text(value: str) -> str:
    """Admin-editable free text must never break out of its APRS-IS line."""
    return _CTRL.sub("", value)


def _call(value: str) -> str:
    if not _CALL_SSID.fullmatch(value):
        raise ValueError(f"invalid iGate callsign: {value!r}")
    return value


def login_line(igate_call: str, filter_: str, version: str) -> str:
    igate_call, filter_, version = _call(igate_call), _text(filter_), _text(version)
    line = f"user {igate_call} pass {passcode(igate_call)} vers OE5XRX-StationManager {version}"
    if filter_:
        line += f" filter {filter_}"
    return line + "\r\n"


def _is_nogate(element: str) -> bool:
    return element.rstrip("*").upper().startswith(NOGATE_TOKENS)


def _split_tnc2(raw: str) -> tuple[str, str]:
    header, _, info = raw.partition(":")
    return header, info


def gate_line(
    packet: "AprsPacket", igate_call: str, own_calls: set[str]
) -> tuple[str | None, str]:
    """``(line, "")`` to send, or ``(None, skip_reason)``."""
    igate_call = _call(igate_call)
    if packet.source.upper() in own_calls:
        return None, "own_call"
    if any(_is_nogate(p) for p in packet.path):
        return None, "nogate_path"
    _, info = _split_tnc2(packet.raw_tnc2)
    info = info.rstrip("\r\n")  # many TNCs terminate the info field with CR
    if any(c in info for c in "\r\n\0"):
        return None, "bad_chars"
    try:
        info.encode("latin-1")  # AX.25 payload bytes only; anything else is not from the agent
    except UnicodeEncodeError:
        return None, "bad_chars"
    # The header fields come from untrusted RF and are re-gated verbatim: refuse q-constructs
    # and anything that is not a plain callsign-like element.
    own_header = ",".join((packet.destination, *packet.path))
    if not valid_header(f"{packet.source}>{own_header}"):
        return None, "bad_header"
    if info.startswith("?"):
        return None, "query"

    if info.startswith("}"):
        inner = parse_thirdparty(info.encode("latin-1"))
        if inner is None:
            return None, "thirdparty_bad"
        header, inner_info = inner
        inner_path = header.partition(">")[2].split(",")[1:]
        if any(_is_nogate(e) for e in inner_path):
            return None, "thirdparty_inet"
        if header.split(">", 1)[0].upper() in own_calls:
            return None, "own_call"
        if inner_info.startswith("?"):
            return None, "query"
        return f"{header},qAR,{igate_call}:{inner_info}\r\n", ""

    header = ",".join((packet.destination, *packet.path))
    return f"{packet.source}>{header},qAR,{igate_call}:{info}\r\n", ""


def _ddmm(value: float, deg_width: int) -> str:
    value = abs(value)
    deg = int(value)
    minutes = round((value - deg) * 60, 2)
    if minutes >= 60:
        deg, minutes = deg + 1, 0.0
    return f"{deg:0{deg_width}d}{minutes:05.2f}"


def beacon_line(igate_call: str, lat: float, lon: float, comment: str) -> str:
    igate_call, comment = _call(igate_call), _text(comment)
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{igate_call}>{TOCALL},TCPIP*:!{_ddmm(lat, 2)}{ns}R{_ddmm(lon, 3)}{ew}&{comment}\r\n"
