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

    async def _connect(self, port: int, connect, now, sleep):
        deadline = now() + CONNECT_GIVE_UP_S
        while True:
            try:
                return await connect("127.0.0.1", port)
            except OSError:
                if now() >= deadline:
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
    ) -> None:
        port = self.KISS_PORT_BASE + slot
        while True:
            reader, writer = await self._connect(port, connect, now, sleep)
            deframer = KissDeframer()
            try:
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
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
