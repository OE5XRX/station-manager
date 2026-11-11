"""APRS decoder plugin: reads KISS frames from the Direwolf unit's local TCP port."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable

from .kiss import KissDeframer

logger = logging.getLogger(__name__)

CONNECT_RETRY_S = 0.5
CONNECT_GIVE_UP_S = 20.0
# Consecutive sessions that end without a single byte (listener accepts, then closes).
MAX_EMPTY_SESSIONS = 5


class AprsDecoder:
    name = "aprs"
    KISS_PORT_BASE = 8100

    def env(self, slot: int, params: dict, rx_node: str) -> dict[str, str]:
        return {
            "OE5XRX_DECODER": self.name,
            "OE5XRX_SLOT": str(slot),
            "OE5XRX_RX_NODE": rx_node,
            "OE5XRX_KISS_PORT": str(self.KISS_PORT_BASE + slot),
            "OE5XRX_SAMPLE_RATE": "48000",
        }

    async def _connect(self, port: int, connect, monotonic, sleep):
        # Monotonic: an NTP step at boot (CM4 has no RTC) must not distort the give-up.
        deadline = monotonic() + CONNECT_GIVE_UP_S
        while True:
            try:
                return await connect("127.0.0.1", port)
            except OSError:
                if monotonic() >= deadline:
                    raise ConnectionError(f"no KISS listener on 127.0.0.1:{port}") from None
                await sleep(CONNECT_RETRY_S)

    async def run(
        self,
        slot: int,
        emit: Callable[[str, bool, dict], Awaitable[None]],
        *,
        connect=asyncio.open_connection,
        now=time.time,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
    ) -> None:
        port = self.KISS_PORT_BASE + slot
        empty_sessions = 0
        while True:
            reader, writer = await self._connect(port, connect, monotonic, sleep)
            deframer = KissDeframer()
            got_data = False
            try:
                while True:
                    try:
                        chunk = await reader.read(4096)
                    except (ConnectionResetError, BrokenPipeError):
                        break  # Direwolf went away: same as EOF, reconnect
                    if not chunk:
                        break
                    got_data = True
                    for frame in deframer.feed(chunk):
                        await emit(
                            "ax25",
                            True,
                            {"ax25": base64.b64encode(frame).decode("ascii"), "heard_at": now()},
                        )
            finally:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001 - best-effort close
                    logger.debug("kiss writer close failed", exc_info=True)
            # Back off after every session so an accept-then-close listener cannot spin the
            # shared control event loop; give up (host reports an error) if it never speaks.
            empty_sessions = 0 if got_data else empty_sessions + 1
            if empty_sessions >= MAX_EMPTY_SESSIONS:
                raise ConnectionError(f"KISS listener on 127.0.0.1:{port} closes immediately")
            await sleep(CONNECT_RETRY_S)
