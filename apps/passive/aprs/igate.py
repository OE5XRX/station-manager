"""APRS-IS gating rules, passcode and beacon: pure functions, no I/O.

RX-only iGate (RF -> IS): the path gets ``qAR,<igate>`` appended; packets that already
travelled over the internet or ask not to be gated are skipped with a reason."""

from typing import TYPE_CHECKING

from .parse import parse_thirdparty

if TYPE_CHECKING:
    from ..models import AprsPacket

NOGATE_TOKENS = ("TCPIP", "TCPXX", "NOGATE", "RFONLY")
TOCALL = "APZXRX"
_INET_TOKENS = ("TCPIP", "TCPXX")


def passcode(callsign: str) -> int:
    """The standard APRS-IS hash over the upper-case call without SSID."""
    call = callsign.upper().split("-", 1)[0]
    h = 0x73E2
    for i in range(0, len(call), 2):
        h ^= ord(call[i]) << 8
        if i + 1 < len(call):
            h ^= ord(call[i + 1])
    return h & 0x7FFF


def login_line(igate_call: str, filter_: str, version: str) -> str:
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
    if packet.source.upper() in own_calls:
        return None, "own_call"
    if any(_is_nogate(p) for p in packet.path):
        return None, "nogate_path"
    _, info = _split_tnc2(packet.raw_tnc2)
    if any(c in info for c in "\r\n\0"):
        return None, "bad_chars"
    if info.startswith("?"):
        return None, "query"

    if info.startswith("}"):
        inner = parse_thirdparty(info.encode("latin-1"))
        if inner is None:
            return None, "thirdparty_bad"
        header, inner_info = inner
        if any(tok in header.upper() for tok in _INET_TOKENS):
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
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{igate_call}>{TOCALL},TCPIP*:!{_ddmm(lat, 2)}{ns}R{_ddmm(lon, 3)}{ew}&{comment}\r\n"
