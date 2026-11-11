"""Minimal APRS information-field parser (APRS101 ch. 8-10).

Own implementation (``aprslib`` is unmaintained). Never raises: anything malformed or
unsupported becomes ``packet_type="other"`` without a position."""

import re
from dataclasses import dataclass

COMMENT_MAX = 256
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_UNCOMPRESSED = re.compile(
    r"(\d{2})([\d ]{2}\.[\d ]{2})([NS])(.)(\d{3})([\d ]{2}\.[\d ]{2})([EW])(.)", re.ASCII
)
MAX_PATH = 8
_HEADER_ELEMENT = re.compile(r"[A-Za-z0-9]{1,9}(-[0-9]{1,2})?\*?", re.ASCII)


@dataclass
class AprsInfo:
    packet_type: str
    latitude: float | None
    longitude: float | None
    symbol: str
    comment: str


def _clean(text: str) -> str:
    return _CTRL.sub("", text)[:COMMENT_MAX]


def _other() -> AprsInfo:
    return AprsInfo("other", None, None, "", "")


def _num(s: str) -> float:
    return float(s.replace(" ", "0"))


def _valid(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _base91(s: str) -> int:
    n = 0
    for ch in s:
        v = ord(ch) - 33
        if not 0 <= v < 91:
            raise ValueError("bad base91")
        n = n * 91 + v
    return n


def _position_body(s: str) -> tuple[float, float, str, str] | None:
    """Position starting at the symbol-table/lat field: ``(lat, lon, symbol, rest)``."""
    m = _UNCOMPRESSED.match(s)
    if m:
        if _num(m[2]) >= 60 or _num(m[6]) >= 60:
            return None  # minutes out of range
        lat = int(m[1]) + _num(m[2]) / 60
        lon = int(m[5]) + _num(m[6]) / 60
        if m[3] == "S":
            lat = -lat
        if m[7] == "W":
            lon = -lon
        return lat, lon, m[4] + m[8], s[m.end() :]
    if len(s) >= 13 and (s[0] in "/\\" or "A" <= s[0] <= "Z" or "a" <= s[0] <= "j"):
        try:
            lat = 90 - _base91(s[1:5]) / 380926
            lon = -180 + _base91(s[5:9]) / 190463
        except ValueError:
            return None
        return lat, lon, s[0] + s[9], s[13:]
    return None


def _positioned(kind: str, body: str | None) -> AprsInfo:
    pos = _position_body(body) if body is not None else None
    if pos is None or not _valid(pos[0], pos[1]):
        return AprsInfo(kind, None, None, "", _clean(body or ""))
    lat, lon, symbol, rest = pos
    return AprsInfo(kind, lat, lon, symbol, _clean(rest))


def _mic_e(info: str, destination: str) -> AprsInfo | None:
    if len(info) < 9 or len(destination) < 6:
        return None
    digits = []
    for ch in destination[:6]:
        if "0" <= ch <= "9":
            digits.append(int(ch))
        elif "A" <= ch <= "J":
            digits.append(ord(ch) - 65)
        elif "P" <= ch <= "Y":
            digits.append(ord(ch) - 80)
        elif ch in "KLZ":
            digits.append(0)
        else:
            return None
    if digits[0] * 10 + digits[1] > 90 or digits[2] * 10 + digits[3] >= 60:
        return None  # latitude degrees/minutes out of range
    north = destination[3] >= "P"
    lon_offset = destination[4] >= "P"
    west = destination[5] >= "P"
    lat = (
        digits[0] * 10
        + digits[1]
        + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100) / 60
    )
    d = ord(info[1]) - 28 + (100 if lon_offset else 0)
    if 180 <= d <= 189:
        d -= 80
    elif 190 <= d <= 199:
        d -= 190
    m = ord(info[2]) - 28
    if m >= 60:
        m -= 60
    h = ord(info[3]) - 28
    if not (0 <= d <= 179 and 0 <= m < 60 and 0 <= h < 100):
        return None
    lon = d + (m + h / 100) / 60
    lat = lat if north else -lat
    lon = -lon if west else lon
    if not _valid(lat, lon):
        return None
    return AprsInfo("mic_e", lat, lon, info[8] + info[7], _clean(info[9:]))


def _parse(info: bytes, destination: str) -> AprsInfo:
    if not info:
        return _other()
    text = info.decode("latin-1")
    dti, body = text[0], text[1:]
    if dti in "!=":
        return _positioned("position", body)
    if dti in "/@":
        return _positioned("position", body[7:] if len(body) > 7 else None)
    if dti in "`'":
        return _mic_e(text, destination) or _other()
    if dti == ";":  # object: name(9) live/killed(1) timestamp(7) position
        return _positioned("object", body[17:] if len(body) > 17 else None)
    if dti == ")":  # item: name(3-9) terminated by ! or _
        end = min((i for i in (body.find("!"), body.find("_")) if i >= 0), default=-1)
        if end < 3 or end > 9:
            return _other()
        return _positioned("item", body[end + 1 :])
    if dti == ">":
        return AprsInfo("status", None, None, "", _clean(body))
    if dti == ":":
        return AprsInfo("message", None, None, "", _clean(body))
    if dti == "}":
        return AprsInfo("thirdparty", None, None, "", _clean(body))
    if dti == "_":
        return AprsInfo("weather", None, None, "", _clean(body))
    return _other()


def parse_info(info: bytes, destination: str) -> AprsInfo:
    try:
        return _parse(info, destination)
    except Exception:
        return _other()


def valid_header(header: str) -> bool:
    """``SRC>DST[,P1..P8]`` with plain callsign-like elements. The header is re-gated
    verbatim, so q-constructs and anything odd (spaces, control chars) are refused."""
    src, sep, rest = header.partition(">")
    if not sep:
        return False
    elements = rest.split(",")
    if len(elements) > 1 + MAX_PATH or not _HEADER_ELEMENT.fullmatch(src):
        return False
    return all(_HEADER_ELEMENT.fullmatch(e) and not e.upper().startswith("Q") for e in elements)


def parse_thirdparty(info: bytes) -> tuple[str, str] | None:
    """``(inner "SRC>DST,PATH", inner info)`` of a ``}`` third-party packet."""
    if not info.startswith(b"}"):
        return None
    text = info[1:].decode("latin-1")
    header, sep, inner = text.partition(":")
    if not sep or not valid_header(header):
        return None
    return header, inner
