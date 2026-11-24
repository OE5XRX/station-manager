"""SSTV decoder unit: ``python3 -m station_agent.sstv.rx_unit --port P --rate R``.

Runs inside the sandboxed ``oe5xrx-decoder@sstv-slotN`` unit. Reads s16le mono
audio from stdin (``pw-record --raw``), decodes it with ``StreamDecoder`` and
serves the events on ``127.0.0.1:P`` as newline-delimited UTF-8 JSON:

    {"t":"start","mode":"<key>","width":W,"height":H}
    {"t":"row","i":N,"rgb":"<base64 of width*3 bytes>"}
    {"t":"end","complete":bool,"rows":N}

One client at a time; a new connection replaces the old one (the agent
reconnects after a hiccup) and, if a picture is being received, first gets
its ``start`` and every row so far again (the agent builds the final picture
from the rows, so a reconnect must not leave holes). Events while
no client is connected are dropped (live only). Exit 0 on stdin EOF (after a
final ``end`` for a running picture), 2 on bad arguments (the unit's
``RestartPreventExitStatus=2``).
"""

import argparse
import asyncio
import base64
import json
import logging
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import decoder as decoder_module
from .decoder import End, Row, Start, StreamDecoder

logger = logging.getLogger(__name__)

SSTV_PORT_BASE = 8200  # port = SSTV_PORT_BASE + slot (APRS uses 8100 + slot)
HOST = "127.0.0.1"
CHUNK_MS = 20
MAX_CLIENT_BUFFER = 4 * 1024 * 1024  # a client this far behind is dropped
DRAIN_TIMEOUT_S = 5.0


class _ArgumentError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # never sys.exit() from inside main()
        raise _ArgumentError(message)


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(value)
    return port


def _rate(value: str) -> int:
    rate = int(value)
    if not 6000 <= rate <= 192000:
        raise ValueError(value)
    return rate


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(prog="python3 -m station_agent.sstv.rx_unit", add_help=False)
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument("--rate", type=_rate, required=True)
    return parser.parse_args(argv)


def _line(event) -> bytes:
    if isinstance(event, Row):
        obj = {"t": "row", "i": event.index, "rgb": base64.b64encode(event.rgb).decode("ascii")}
    elif isinstance(event, Start):
        m = event.mode
        obj = {"t": "start", "mode": m.key, "width": m.width, "height": m.height}
    else:
        obj = {"t": "end", "complete": event.complete, "rows": event.rows}
    return json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"


def _drop(writer: asyncio.StreamWriter) -> None:
    """Close without flushing. ``close()`` keeps a non-reading connection open
    until its buffer drains, and ``server.wait_closed()`` waits for every
    connection, so a stalled client would keep the unit from ever exiting."""
    writer.transport.abort()


class RxUnit:
    def __init__(self, port: int, rate: int, stdin):
        self.port = port
        self.rate = rate
        self._stdin = stdin
        self._decoder = StreamDecoder(rate)
        self._chunk_bytes = rate * CHUNK_MS // 1000 * 2
        self._rest = b""
        self._client: asyncio.StreamWriter | None = None
        # Lines of the picture being received (start + rows), replayed to a new
        # client. Bounded by the largest picture: PD 180, ~1.3 MB.
        self._replay: list[bytes] = []
        self.addresses: list[tuple[str, int]] = []

    @property
    def has_client(self) -> bool:
        return self._client is not None

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        old, self._client = self._client, writer
        if old is not None:
            _drop(old)
        if self._replay:
            writer.write(b"".join(self._replay))
        try:
            while await reader.read(4096):  # input is ignored; wait for EOF
                pass
        except (ConnectionError, OSError):
            pass
        finally:
            if self._client is writer:
                self._client = None
            if writer.transport.get_write_buffer_size():
                _drop(writer)  # peer half-closed and not reading
            else:
                writer.close()

    def _send(self, lines: list[bytes]) -> None:
        writer = self._client
        if writer is None or not lines:
            return
        if writer.is_closing() or writer.transport.get_write_buffer_size() > MAX_CLIENT_BUFFER:
            logger.warning("sstv client not reading, dropping it")
            self._client = None
            _drop(writer)
            return
        writer.write(b"".join(lines))

    def _step(self) -> tuple[list, bool]:
        """Read one chunk of stdin and decode it (runs in the executor)."""
        data = self._stdin.read(self._chunk_bytes)
        if not data:
            return self._decode(self._decoder.flush), True
        data = self._rest + data
        usable = len(data) & ~1
        self._rest = data[usable:]
        samples = np.frombuffer(data[:usable], dtype="<i2")
        return self._decode(lambda: self._decoder.feed(samples)), False

    def _decode(self, call) -> list:
        """Last line of defence: a decoder bug ends the running picture
        (incomplete) and restarts the decoder instead of killing the unit."""
        try:
            return call()
        except Exception:
            if decoder_module.STRICT:
                raise
            logger.exception("sstv decoder failed; restarting it")
            self._decoder = StreamDecoder(self.rate)
            if self._replay:  # a picture is running: start line + its rows
                return [End(complete=False, rows=len(self._replay) - 1)]
            return []

    def _publish(self, events: list) -> None:
        """Serialise and send (event loop thread, so a reconnect never races it)."""
        lines = []
        for event in events:
            line = _line(event)
            if isinstance(event, Start):
                self._replay = [line]
                logger.info("sstv picture started: %s", event.mode.key)
            elif isinstance(event, End):
                self._replay = []
                logger.info("sstv picture ended: complete=%s rows=%d", event.complete, event.rows)
            else:
                self._replay.append(line)
            lines.append(line)
        self._send(lines)

    async def _close_client(self) -> None:
        """At EOF: give the client up to DRAIN_TIMEOUT_S for the final lines, then close."""
        writer, self._client = self._client, None
        if writer is None:
            return
        try:
            await asyncio.wait_for(writer.drain(), DRAIN_TIMEOUT_S)
        except (TimeoutError, ConnectionError, OSError):
            logger.warning("sstv client did not take the final lines in time")
            _drop(writer)
            return
        writer.close()

    async def run(self, started: Callable[["RxUnit"], None] | None = None) -> int:
        server = await asyncio.start_server(self._on_client, HOST, self.port)
        self.addresses = [sock.getsockname()[:2] for sock in server.sockets]
        if started is not None:
            started(self)
        loop = asyncio.get_running_loop()
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sstv-rx") as pool:
                while True:
                    events, eof = await loop.run_in_executor(pool, self._step)
                    self._publish(events)
                    if eof:
                        break
            await self._close_client()
        finally:
            server.close()
            await server.wait_closed()
        return 0


def main(argv: list[str] | None = None, stdin=None, *, started=None) -> int:
    try:
        args = _parse(argv)
    except _ArgumentError as exc:
        print(f"rx_unit: {exc}", file=sys.stderr)
        return 2
    if stdin is None:
        stdin = sys.stdin.buffer
    unit = RxUnit(args.port, args.rate, stdin)
    return asyncio.run(unit.run(started))


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
