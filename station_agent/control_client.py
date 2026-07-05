"""Persistent outbound Control-WebSocket for the Station Agent.

Reuses the proven Ed25519 outbound-WS pattern from terminal.py (signed timestamp +
body-hash query params, exponential reconnect backoff), but is PERSISTENT while the
station is online (design spec §9). It runs the device-agnostic Broker: on connect it
discovers modules, pushes an ``inventory`` snapshot, then relays server ``command`` /
``subscribe`` / ``unsubscribe`` / ``ptt_keepalive`` into the broker and the broker's
``inventory`` / ``state`` / ``result`` / ``event`` back up. A disconnect fires the PTT
dead-man locally.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import threading
import time
from urllib.parse import urlencode

import websockets

from .broker import Broker
from .config import AgentConfig
from .protocol import ProtocolError, parse_message
from .signing import load_private_key
from .slot_control import SlotControl
from .slot_discovery import discover_slots

logger = logging.getLogger(__name__)

BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 60.0
BACKOFF_FACTOR = 2.0


class ControlClient:
    def __init__(self, config: AgentConfig):
        self._config = config
        self._ws = None
        self._shutdown = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._private_key = load_private_key(config.ed25519_key_path)
        if self._private_key is None:
            raise RuntimeError(
                "Control: Ed25519 key could not be loaded; WebSocket authentication is impossible"
            )

    def _build_ws_url(self) -> str:
        server = self._config.server_url
        if server.startswith("https://"):
            ws_base = "wss://" + server[len("https://") :]
        elif server.startswith("http://"):
            ws_base = "ws://" + server[len("http://") :]
        else:
            ws_base = "wss://" + server
        path = f"/ws/agent/control/{self._config.station_id}/"
        timestamp = str(time.time())
        body_hash = hashlib.sha256(b"").hexdigest()
        signature = self._private_key.sign(f"{timestamp}:{body_hash}".encode())
        query = {
            "station_id": str(self._config.station_id),
            "signature": base64.b64encode(signature).decode("ascii"),
            "timestamp": timestamp,
        }
        return f"{ws_base}{path}?{urlencode(query)}"

    async def _ws_send(self, msg: dict) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(msg))

    async def _connect_and_serve(self) -> None:
        url = self._build_ws_url()
        logger.info("Control: connecting to server")
        async with websockets.connect(
            url, ping_interval=30, ping_timeout=10, close_timeout=5
        ) as ws:
            self._ws = ws
            broker = Broker(
                self._ws_send,
                transport_factory=SlotControl,
                dead_man_timeout=getattr(self._config, "control_dead_man_timeout", 1.5),
                telemetry_default_interval_ms=getattr(
                    self._config, "telemetry_default_interval_ms", 1000
                ),
                telemetry_min_floor_ms=getattr(self._config, "telemetry_min_floor_ms", 200),
            )
            loop = asyncio.get_running_loop()
            try:
                discovered = await loop.run_in_executor(
                    None, discover_slots, self._config.slot_dev_base
                )
            except Exception:  # noqa: BLE001 — discovery must not break the control link
                logger.exception("Control: slot discovery failed; reporting empty inventory")
                discovered = []
            broker.set_inventory(discovered)
            await broker.emit_inventory()
            logger.info("Control: connected, inventory sent")

            try:
                async for message in ws:
                    if self._shutdown.is_set():
                        break
                    try:
                        parsed = parse_message(message)
                    except ProtocolError as exc:
                        logger.warning("Control: dropping malformed message: %s", exc)
                        continue
                    await broker.handle(parsed)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.info("Control: WebSocket closed (code=%s)", exc.code)
            finally:
                await broker.on_disconnect()
                self._ws = None

    async def _run_async(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self._shutdown.is_set():
            try:
                await self._connect_and_serve()
                backoff = BACKOFF_INITIAL
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                logger.warning("Control: connection error (%s), retrying in %.0fs", exc, backoff)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Control: unexpected error (%s: %s), retrying in %.0fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
            if self._shutdown.is_set():
                break
            wait_end = time.monotonic() + backoff
            while time.monotonic() < wait_end and not self._shutdown.is_set():
                await asyncio.sleep(0.5)
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
        logger.info("Control: client stopped")

    def run(self) -> None:
        logger.info("Control: starting client")
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._run_async())
        except Exception as exc:  # noqa: BLE001
            logger.error("Control: event loop error: %s", exc)
        finally:
            self._loop.close()
            self._loop = None

    def stop(self) -> None:
        logger.info("Control: stop requested")
        self._shutdown.set()
        if self._ws is not None and self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
