"""AX.25 UI frame -> TNC2 text (the wire format APRS-IS speaks).

Only UI frames (control ``0x03``, PID ``0xF0``) are APRS; everything else is refused."""

from collections.abc import Sequence
from dataclasses import dataclass

MAX_DIGIPEATERS = 8
_ADDR_LEN = 7


class Ax25Error(ValueError):
    pass


@dataclass(frozen=True)
class Ax25Frame:
    source: str
    destination: str
    path: tuple[str, ...]
    info: bytes


def _address(raw: bytes) -> tuple[str, bool, bool]:
    """``(call-ssid, h_bit, last)`` of one 7-byte shifted address."""
    call = "".join(chr(b >> 1) for b in raw[:6]).rstrip(" ")
    if not call or not all(c.isalnum() for c in call):
        raise Ax25Error("invalid callsign in address field")
    ssid = (raw[6] >> 1) & 0x0F
    if ssid:
        call = f"{call}-{ssid}"
    return call, bool(raw[6] & 0x80), bool(raw[6] & 0x01)


def decode_ax25(frame: bytes) -> Ax25Frame:
    addrs: list[tuple[str, bool]] = []
    pos = 0
    while True:
        chunk = frame[pos : pos + _ADDR_LEN]
        if len(chunk) < _ADDR_LEN:
            raise Ax25Error("truncated address field")
        call, h_bit, last = _address(chunk)
        addrs.append((call, h_bit))
        pos += _ADDR_LEN
        if last:
            break
        if len(addrs) >= 2 + MAX_DIGIPEATERS:
            raise Ax25Error("too many digipeaters / no address terminator")
    if len(addrs) < 2:
        raise Ax25Error("missing source address")
    if len(frame) < pos + 2:
        raise Ax25Error("truncated frame")
    if frame[pos] != 0x03 or frame[pos + 1] != 0xF0:
        raise Ax25Error("not a UI frame with PID 0xF0")
    destination, source = addrs[0][0], addrs[1][0]
    digis = addrs[2:]
    last_used = max((i for i, (_, h) in enumerate(digis) if h), default=None)
    path = tuple(f"{call}*" if i == last_used else call for i, (call, _) in enumerate(digis))
    return Ax25Frame(source, destination, path, bytes(frame[pos + 2 :]))


def to_tnc2(f: Ax25Frame) -> str:
    header = ",".join((f.destination, *f.path))
    return f"{f.source}>{header}:{f.info.decode('latin-1')}"


def is_digipeated(path: Sequence[str]) -> bool:
    return any(p.endswith("*") for p in path)
