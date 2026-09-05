"""Persistent outbound Audio-WebSocket for the Station Agent (Spec 0 §5.1).

Mirrors ``control_client.py``: the same proven Ed25519 signed-query-param auth and
exponential reconnect backoff, persistent while the station is online. It carries the §5
audio contract:

- On connect: build the :class:`AudioEngine`, discover audio slots, send ``advertise``.
- Server → agent JSON: ``source_subscribe`` / ``source_unsubscribe`` (demand gating),
  ``mic_state`` (uplink authorized/keyed).
- Binary frames both ways: RX Opus packets → §5.3 media frames out; inbound ``op.mic``
  media frames → TX injection.
- On disconnect: tear down all bridges + the mic dead-man.

The endpoint and auth are identical to control/terminal; only the path differs.
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

from station_agent.audio.engine import AudioEngine
from station_agent.audio.router_backend import PipeWireRouterBackend
from station_agent.protocol import ProtocolError, parse_message
from station_agent.signing import load_private_key

logger = logging.getLogger(__name__)

BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 60.0
BACKOFF_FACTOR = 2.0


class AudioClient:
    def __init__(self, config, *, backend=None, bridge_factory=None):
        self._config = config
        self._backend = backend or PipeWireRouterBackend(
            sysfs_sound=getattr(config, "audio_sysfs_sound", "/sys/class/sound")
        )
        self._bridge_factory = bridge_factory
        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._engine: AudioEngine | None = None
        self._shutdown = threading.Event()
        self._private_key = load_private_key(config.ed25519_key_path)
        if self._private_key is None:
            raise RuntimeError(
                "Audio: Ed25519 key could not be loaded; WebSocket authentication is impossible"
            )

    # --- url / auth (identical scheme to control) --------------------------
    def _build_ws_url(self) -> str:
        server = self._config.server_url
        if server.startswith("https://"):
            ws_base = "wss://" + server[len("https://") :]
        elif server.startswith("http://"):
            ws_base = "ws://" + server[len("http://") :]
        else:
            ws_base = "wss://" + server
        path = f"/ws/agent/audio/{self._config.station_id}/"
        timestamp = str(time.time())
        body_hash = hashlib.sha256(b"").hexdigest()
        signature = self._private_key.sign(f"{timestamp}:{body_hash}".encode())
        query = {
            "station_id": str(self._config.station_id),
            "signature": base64.b64encode(signature).decode("ascii"),
            "timestamp": timestamp,
        }
        return f"{ws_base}{path}?{urlencode(query)}"

    # --- send seams handed to the engine -----------------------------------
    async def _send_json(self, msg: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(msg))
        except websockets.exceptions.ConnectionClosed:
            self._ws = None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Audio: _send_json failed (%s), dropping", type(exc).__name__)

    def _emit_binary(self, data: bytes) -> None:
        """Thread-safe hand-off: RX bridge reader threads call this to ship a media frame.

        Scheduled onto the client's event loop, so the actual ``ws.send`` happens on the
        one thread that owns the socket."""
        loop, ws = self._loop, self._ws
        if loop is None or ws is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._ws_send_binary(data), loop)
        except RuntimeError:
            pass  # loop closing

    async def _ws_send_binary(self, data: bytes) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(data)
        except websockets.exceptions.ConnectionClosed:
            self._ws = None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Audio: binary send failed (%s), dropping", type(exc).__name__)

    # --- connection --------------------------------------------------------
    async def _connect_and_serve(self) -> None:
        url = self._build_ws_url()
        logger.info("Audio: connecting to server")
        async with websockets.connect(
            url, ping_interval=30, ping_timeout=10, close_timeout=5, max_size=None
        ) as ws:
            self._ws = ws
            self._engine = AudioEngine(
                self._backend,
                emit_json=self._send_json,
                emit_binary=self._emit_binary,
                bridge_factory=self._bridge_factory,
                rx_rate=getattr(self._config, "audio_rx_rate", 8000),
                mic_rate=getattr(self._config, "audio_mic_rate", 16000),
                dead_man_timeout=getattr(self._config, "audio_dead_man_timeout", 1.5),
            )
            await self._engine.start()
            logger.info("Audio: connected, advertise sent")
            try:
                async for message in ws:
                    if self._shutdown.is_set():
                        break
                    await self._dispatch(message)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.info("Audio: WebSocket closed (code=%s)", exc.code)
            finally:
                await self._engine.stop()
                self._engine = None
                self._ws = None

    async def _dispatch(self, message) -> None:
        if isinstance(message, (bytes, bytearray)):
            await self._engine.on_media_frame(bytes(message))
            return
        try:
            msg = parse_message(message)
        except ProtocolError as exc:
            logger.warning("Audio: dropping malformed message: %s", exc)
            return
        mtype = msg.get("type")
        if mtype == "source_subscribe":
            await self._engine.on_source_subscribe(msg.get("stream_id"))
        elif mtype == "source_unsubscribe":
            await self._engine.on_source_unsubscribe(msg.get("stream_id"))
        elif mtype == "mic_state":
            await self._engine.on_mic_state(
                active=bool(msg.get("active")),
                tx_slot=msg.get("tx_slot"),
                tx_module=msg.get("tx_module"),
            )
        else:
            logger.debug("Audio: ignoring message type %r", mtype)

    async def _run_async(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self._shutdown.is_set():
            try:
                await self._connect_and_serve()
                backoff = BACKOFF_INITIAL
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                logger.warning("Audio: connection error (%s), retrying in %.0fs", exc, backoff)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Audio: unexpected error (%s: %s), retrying in %.0fs",
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
        logger.info("Audio: client stopped")

    def run(self) -> None:
        logger.info("Audio: starting client")
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._run_async())
        except Exception as exc:  # noqa: BLE001
            logger.error("Audio: event loop error: %s", exc)
        finally:
            self._loop.close()
            self._loop = None

    def stop(self) -> None:
        logger.info("Audio: stop requested")
        self._shutdown.set()
        if self._ws is not None and self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
