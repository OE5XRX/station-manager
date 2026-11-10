"""KISS de-framing (TNC byte stream -> AX.25 payloads)."""

from __future__ import annotations

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD
MAX_FRAME = 1024


class KissDeframer:
    """Incremental KISS parser. Yields port-0 data frames only (command byte 0x00)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._overflow = False

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        for b in data:
            if b == FEND:
                frame = None if self._overflow else bytes(self._buf)
                self._buf.clear()
                self._overflow = False
                if frame:
                    payload = self._decode(frame)
                    if payload is not None:
                        out.append(payload)
                continue
            if self._overflow:
                continue
            self._buf.append(b)
            # Escapes at most double the size (command byte + 2 raw bytes per decoded one),
            # so this bounds the buffer without dropping a valid escape-heavy frame; the
            # decoded length is checked against MAX_FRAME in _decode.
            if len(self._buf) > 2 * MAX_FRAME + 1:
                self._buf.clear()
                self._overflow = True
        return out

    @staticmethod
    def _decode(frame: bytes) -> bytes | None:
        if frame[0] != 0x00:
            return None
        res = bytearray()
        esc = False
        for b in frame[1:]:
            if esc:
                res.append(FEND if b == TFEND else FESC if b == TFESC else b)
                esc = False
            elif b == FESC:
                esc = True
            else:
                res.append(b)
        if len(res) > MAX_FRAME or not res:
            return None
        return bytes(res)
