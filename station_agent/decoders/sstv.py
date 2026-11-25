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
# The server (daphne) closes the control WebSocket on a message above 1 MiB, and a reliable
# frame that never fits would be re-sent from the spool after every reconnect. A noisy
# PD 120/180 picture (640x496 RGB) is ~1.3 MB as base64 PNG, so the PNG is re-encoded with
# fewer bits per channel until its base64 fits this budget (leaves room for the JSON frame).
MAX_PNG_B64 = 900_000
# Bits per channel tried in order (pure noise fits at 5 bits; 2 bits always fits).
DEPTHS = (8, 6, 5, 4, 3, 2)

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
    # until the replay reaches rows we did not have yet; ``verified`` = indices that matched.
    verifying: bool = False
    verified: set[int] = field(default_factory=set)


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
        # slot -> image emits in flight (encoding a finished picture); finalize waits for them.
        self._closing: dict[int, set[asyncio.Task]] = {}
        # Slots whose decoder is being stopped: the reader's further lines are ignored.
        self._finalizing: set[int] = set()

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
        self._finalizing.discard(slot)
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
        if slot in self._finalizing:
            return  # being stopped: no new picture, no rows the final image cannot carry
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
            picture = self._pictures.get(slot)
            if picture is None:
                return
            if picture.verifying and set(picture.rows) - picture.verified:
                # The unit's picture ended without the rows we hold beyond the replay: it is
                # not ours (ours ended unseen). Ours goes out partial, the replay is the new
                # one — unless it is too small to keep (no ghost sstv_start for it).
                picture = await self._split(slot, picture, emit, keep_new=False)
                if picture is None:
                    return
            if self._pictures.get(slot) is not picture:
                return  # finalize took it while we were waiting
            del self._pictures[slot]
            complete = msg.get("complete") is True and len(picture.rows) == picture.height
            await self._close(slot, picture, complete, emit)

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
                current.verified = set()
                return
            # A new picture while one is open: its "end" got lost (e.g. while disconnected).
            del self._pictures[slot]
            await self._close(slot, current, False, emit)
        await self._new_picture(slot, mode.key, emit)

    async def _new_picture(self, slot, mode_key, emit, rows=None) -> _Picture | None:
        """Open a picture; None if the decoder is being stopped (checked here because
        every caller may have awaited an image emit, during which finalize ran)."""
        if slot in self._finalizing:
            return None
        mode = MODES[mode_key]
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
        for index in sorted(picture.rows):
            if self._pictures.get(slot) is not picture:
                return None  # finalize took it while an emit was suspended
            await self._line(picture, index, picture.rows[index], emit)
        return picture if self._pictures.get(slot) is picture else None

    async def _split(
        self, slot: int, old: _Picture, emit: Emit, *, keep_new: bool = True
    ) -> _Picture | None:
        """The replay is a different picture than ``old``: finalise ``old`` partial and
        start a new picture from the replayed rows (the verified ones are identical).
        ``keep_new=False`` (the replay already ended): skip a new picture below the 10 %
        rule. None when no new picture was opened."""
        self._pictures.pop(slot, None)
        await self._close(slot, old, False, emit)
        rows = {i: old.rows[i] for i in old.verified}
        if not keep_new and len(rows) < MIN_ROWS_FRACTION * old.height:
            return None
        return await self._new_picture(slot, old.mode, emit, rows)

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
            if stored == rgb:
                picture.verified.add(index)  # replayed row we already hold (and showed)
                return
            if stored is not None or any(i < index for i in set(picture.rows) - picture.verified):
                # A row differs, or the replay skipped a row we hold: not our picture.
                picture = await self._split(slot, picture, emit)
                if picture is None:
                    return
            else:
                picture.verifying = False  # past what we had: the replay matched
        if picture.rows.get(index) == rgb:
            return  # repeated row: nothing new to show
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

    # -- final picture ----------------------------------------------------------------

    async def _close(self, slot: int, picture: _Picture, complete: bool, emit: Emit) -> None:
        await self._background(slot, self._emit_image(picture, complete, emit))

    async def _background(self, slot: int, coro) -> None:
        """Run an image emit in its own task: neither a reader cancelled mid-encode nor the
        host's finalize timeout loses the picture (it is spooled when done)."""
        task = asyncio.ensure_future(coro)
        tasks = self._closing.setdefault(slot, set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        await asyncio.shield(task)

    async def _emit_image(self, picture: _Picture, complete: bool, emit: Emit) -> None:
        if len(picture.rows) < MIN_ROWS_FRACTION * picture.height:
            logger.info(
                "sstv picture %s discarded: %d/%d rows",
                picture.picture_id,
                len(picture.rows),
                picture.height,
            )
            return
        rows = dict(picture.rows)
        # zlib on ~1 MB (PD 120/180) takes a while on a CM4: off the control event loop.
        png_b64, depth = await asyncio.get_running_loop().run_in_executor(
            self._executor, _encode, picture.width, picture.height, rows
        )
        await emit(
            "sstv_image",
            True,
            {
                "picture_id": picture.picture_id,
                "mode": picture.mode,
                "complete": complete,
                "rows": len(rows),
                "heard_at": picture.heard_at,
                "depth": depth,
                "png": png_b64,
            },
        )

    async def finalize(self, slot: int, emit: Emit) -> None:
        """The host is stopping this decoder: save the picture so far (partial)."""
        self._finalizing.add(slot)
        picture = self._pictures.pop(slot, None)
        pending = list(self._closing.get(slot, ()))

        async def last() -> None:
            if pending:  # a picture that just ended is still being encoded: keep the order
                await asyncio.wait(pending)
            if picture is not None:
                await self._emit_image(picture, False, emit)

        await self._background(slot, last())


def _encode(width: int, height: int, rows: dict[int, bytes]) -> tuple[str, int]:
    """Base64 PNG of the rows (missing ones black) and the bits per channel it kept."""
    black = bytes(width * 3)
    rgb = b"".join(rows.get(y, black) for y in range(height))
    for depth in DEPTHS:
        data = rgb if depth == 8 else rgb.translate(_MASKS[depth])
        png_b64 = base64.b64encode(encode_png(width, height, data)).decode("ascii")
        if len(png_b64) <= MAX_PNG_B64:
            return png_b64, depth
    raise ValueError(f"sstv picture {width}x{height} does not fit {MAX_PNG_B64} bytes")


# Byte tables keeping the top ``depth`` bits of every channel value.
_MASKS = {d: bytes(v & (0xFF << (8 - d)) & 0xFF for v in range(256)) for d in DEPTHS}
