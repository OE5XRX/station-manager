"""SSTV decoder plugin: reads the decoder unit's NDJSON events from its local TCP port.

The unit (``station_agent.sstv.rx_unit``, numpy, sandboxed) does the decoding; this plugin
runs in the agent and must stay stdlib-only (no numpy, no ``station_agent.sstv.decoder``).
It turns the unit's ``start``/``row``/``end`` lines into ``decoder_output`` frames:

- ``sstv_start`` (reliable) once per picture,
- ``sstv_line`` (unreliable, live view only) per new row,
- ``sstv_image`` (reliable, authoritative) with the whole picture as PNG when the picture
  ends, or — partial, ``complete: false`` — when the host stops the decoder (``finalize``).
  Pictures with fewer than 10 % of their rows are discarded.

After a reconnect the unit replays ``start`` and every row of the running picture. A replay
of the picture we already hold keeps its ``picture_id`` (no second picture); it is
recognised by the mode and by the replayed rows matching the stored ones byte for byte
(the decoder is deterministic). A mismatch means a different picture: the old one is
finalised partial and the new one gets its own id.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from station_agent.sstv.modes import MODES
from station_agent.sstv.png import encode_png

logger = logging.getLogger(__name__)

# Must match station_agent.sstv.rx_unit.SSTV_PORT_BASE (not imported: that module needs numpy).
SSTV_PORT_BASE = 8200
SAMPLE_RATE = 8000
MIN_ROWS_FRACTION = 0.1
CONNECT_RETRY_S = 0.5
CONNECT_GIVE_UP_S = 20.0
# Consecutive sessions that end without a single line (listener accepts, then closes).
MAX_EMPTY_SESSIONS = 5
# A line is at most one row of PD 120/180: 640 * 3 bytes base64 + JSON, far below this.
MAX_LINE_BYTES = 64 * 1024

Emit = Callable[[str, bool, dict], Awaitable[None]]


@dataclass
class _Picture:
    picture_id: str
    mode: str
    width: int
    height: int
    heard_at: float
    rows: dict[int, bytes] = field(default_factory=dict)
    # Set by a replayed "start" after a reconnect: rows are compared with the stored ones
    # until the replay reaches rows we did not have yet.
    verifying: bool = False


def _valid_start(msg: dict):
    mode = MODES.get(msg.get("mode")) if isinstance(msg.get("mode"), str) else None
    if mode is None or msg.get("width") != mode.width or msg.get("height") != mode.height:
        return None
    return mode


class SstvDecoderPlugin:
    name = "sstv"
    # SSTV must never run on a station nobody holds: the server frees the lock when the
    # agent link drops, so the host stops this decoder then (APRS keeps running).
    stop_on_link_loss = True

    def __init__(
        self,
        *,
        connect=asyncio.open_connection,
        now=time.time,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
        executor=None,
    ):
        self._connect_fn = connect
        self._now = now
        self._sleep = sleep
        self._monotonic = monotonic
        self._executor = executor
        self._pictures: dict[int, _Picture] = {}  # slot -> picture being received

    def env(self, slot: int, params: dict, rx_node: str) -> dict[str, str]:
        return {
            "OE5XRX_DECODER": self.name,
            "OE5XRX_SLOT": str(slot),
            "OE5XRX_RX_NODE": rx_node,
            "OE5XRX_SSTV_PORT": str(SSTV_PORT_BASE + slot),
            "OE5XRX_SAMPLE_RATE": str(SAMPLE_RATE),
        }

    # -- connection ---------------------------------------------------------------

    async def _connect(self, port: int):
        # Monotonic: an NTP step at boot (CM4 has no RTC) must not distort the give-up.
        deadline = self._monotonic() + CONNECT_GIVE_UP_S
        while True:
            try:
                return await self._connect_fn("127.0.0.1", port, limit=MAX_LINE_BYTES)
            except OSError:
                if self._monotonic() >= deadline:
                    raise ConnectionError(f"no SSTV listener on 127.0.0.1:{port}") from None
                await self._sleep(CONNECT_RETRY_S)

    async def run(self, slot: int, emit: Emit) -> None:
        self._pictures.pop(slot, None)  # never inherit a picture from an earlier run
        port = SSTV_PORT_BASE + slot
        empty_sessions = 0
        while True:
            reader, writer = await self._connect(port)
            got_data = False
            first = True
            try:
                while True:
                    try:
                        line = await reader.readline()
                    except (ConnectionResetError, BrokenPipeError):
                        break  # the unit went away: same as EOF, reconnect
                    except ValueError:  # over-long line: the stream is out of step
                        logger.warning("sstv slot %s: over-long line, reconnecting", slot)
                        break
                    if not line.endswith(b"\n"):
                        break  # EOF (a partial last line is dropped)
                    got_data = True
                    await self._handle(slot, line, emit, first)
                    first = False
            finally:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001 - best-effort close
                    logger.debug("sstv writer close failed", exc_info=True)
            # Back off after every session so an accept-then-close listener cannot spin the
            # shared control event loop; give up (host reports an error) if it never speaks.
            empty_sessions = 0 if got_data else empty_sessions + 1
            if empty_sessions >= MAX_EMPTY_SESSIONS:
                raise ConnectionError(f"SSTV listener on 127.0.0.1:{port} closes immediately")
            await self._sleep(CONNECT_RETRY_S)

    # -- events -------------------------------------------------------------------

    async def _handle(self, slot: int, line: bytes, emit: Emit, first: bool) -> None:
        try:
            msg = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            logger.warning("sstv slot %s: dropping malformed line", slot)
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("t")
        if kind == "start":
            await self._on_start(slot, msg, emit, replay=first)
        elif kind == "row":
            await self._on_row(slot, msg, emit)
        elif kind == "end":
            picture = self._pictures.pop(slot, None)
            if picture is not None:
                complete = msg.get("complete") is True and len(picture.rows) == picture.height
                await self._emit_image(picture, complete, emit)

    async def _on_start(self, slot: int, msg: dict, emit: Emit, *, replay: bool) -> None:
        mode = _valid_start(msg)
        if mode is None:
            logger.warning("sstv slot %s: dropping invalid start %r", slot, msg)
            return
        current = self._pictures.get(slot)
        if current is not None:
            if replay and current.mode == mode.key:
                # Probably the unit replaying our picture after a reconnect: keep it and
                # check the replayed rows against what we have.
                current.verifying = True
                return
            # A new picture while one is open: its "end" got lost (e.g. while disconnected).
            del self._pictures[slot]
            await self._emit_image(current, False, emit)
        await self._new_picture(slot, mode, emit)

    async def _new_picture(self, slot, mode, emit, rows=None) -> _Picture:
        picture = _Picture(uuid.uuid4().hex, mode.key, mode.width, mode.height, self._now())
        if rows:
            picture.rows.update(rows)
        self._pictures[slot] = picture
        await emit(
            "sstv_start",
            True,
            {
                "picture_id": picture.picture_id,
                "mode": picture.mode,
                "width": picture.width,
                "height": picture.height,
            },
        )
        return picture

    async def _on_row(self, slot: int, msg: dict, emit: Emit) -> None:
        picture = self._pictures.get(slot)
        index, b64 = msg.get("i"), msg.get("rgb")
        if (
            picture is None
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < picture.height
            or not isinstance(b64, str)
        ):
            return
        try:
            rgb = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return
        if len(rgb) != picture.width * 3:
            return
        if picture.verifying:
            stored = picture.rows.get(index)
            if stored is None:
                picture.verifying = False  # past what we had: the replay matched
            elif stored == rgb:
                return  # replayed row we already hold (and already showed)
            else:
                # Different picture than ours (ours ended unseen): finalise ours, keep the
                # replayed rows that matched (they are the new picture's) and go on.
                del self._pictures[slot]
                await self._emit_image(picture, False, emit)
                mode = MODES[picture.mode]
                kept = {i: r for i, r in picture.rows.items() if i < index}
                picture = await self._new_picture(slot, mode, emit, kept)
                for i in sorted(kept):
                    await self._line(picture, i, kept[i], emit)
        picture.rows[index] = rgb
        await self._line(picture, index, rgb, emit)

    async def _line(self, picture: _Picture, index: int, rgb: bytes, emit: Emit) -> None:
        await emit(
            "sstv_line",
            False,
            {
                "picture_id": picture.picture_id,
                "row": index,
                "rgb": base64.b64encode(rgb).decode("ascii"),
            },
        )

    async def _emit_image(self, picture: _Picture, complete: bool, emit: Emit) -> None:
        if len(picture.rows) < MIN_ROWS_FRACTION * picture.height:
            logger.info(
                "sstv picture %s discarded: %d/%d rows",
                picture.picture_id,
                len(picture.rows),
                picture.height,
            )
            return
        stride = picture.width * 3
        black = bytes(stride)  # missing rows stay black
        rgb = b"".join(picture.rows.get(y, black) for y in range(picture.height))
        # zlib on ~1 MB (PD 120/180) takes a while on a CM4: off the control event loop.
        png = await asyncio.get_running_loop().run_in_executor(
            self._executor, encode_png, picture.width, picture.height, rgb
        )
        await emit(
            "sstv_image",
            True,
            {
                "picture_id": picture.picture_id,
                "mode": picture.mode,
                "complete": complete,
                "rows": len(picture.rows),
                "heard_at": picture.heard_at,
                "png": base64.b64encode(png).decode("ascii"),
            },
        )

    async def finalize(self, slot: int, emit: Emit) -> None:
        """The host is stopping this decoder: save the picture so far (partial)."""
        picture = self._pictures.pop(slot, None)
        if picture is not None:
            await self._emit_image(picture, False, emit)
