"""Remote terminal (Web Shell) for the Station Agent.

Connects to the server via WebSocket and provides shell access
through a pseudo-terminal. An admin can open a browser-based
terminal that streams stdin/stdout over the WebSocket.
"""

import asyncio
import base64
import codecs
import fcntl
import hashlib
import json
import logging
import os
import pty
import struct
import subprocess
import termios
import threading
import time
from urllib.parse import urlencode

import websockets

from .config import AgentConfig
from .signing import load_private_key

logger = logging.getLogger(__name__)

# base64 is used below only for the Ed25519 signature query param.
# PTY I/O crosses three layers (agent <-> server <-> browser) as plain
# UTF-8 strings inside the JSON `data` field. A stateful incremental
# decoder buffers any incomplete multi-byte codepoint that straddles a
# read-chunk boundary and feeds its tail into the next decode call, so
# no legitimate character is ever replaced with U+FFFD just because
# os.read() split it.

# Reconnect backoff settings
BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 60.0
BACKOFF_FACTOR = 2.0


class TerminalClient:
    """WebSocket client that provides remote shell access to the station."""

    def __init__(self, config: AgentConfig):
        self._config = config
        self._process: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._ws = None
        self._shutdown = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._private_key = load_private_key(config.ed25519_key_path)
        if self._private_key is None:
            raise RuntimeError(
                "Terminal: Ed25519 key could not be loaded; WebSocket authentication is impossible"
            )

    def _build_ws_url(self) -> str:
        """Build the WebSocket URL with authentication query parameters."""
        server = self._config.server_url
        if server.startswith("https://"):
            ws_base = "wss://" + server[len("https://") :]
        elif server.startswith("http://"):
            ws_base = "ws://" + server[len("http://") :]
        else:
            ws_base = "wss://" + server

        path = f"/ws/agent/terminal/{self._config.station_id}/"

        # Build auth query params by signing an empty body
        query_params = {"station_id": str(self._config.station_id)}

        timestamp = str(time.time())
        body_hash = hashlib.sha256(b"").hexdigest()
        signed_data = f"{timestamp}:{body_hash}".encode()
        signature = self._private_key.sign(signed_data)
        signature_b64 = base64.b64encode(signature).decode("ascii")
        query_params["signature"] = signature_b64
        query_params["timestamp"] = timestamp

        return f"{ws_base}{path}?{urlencode(query_params)}"

    def _start_shell(self) -> tuple[int, subprocess.Popen]:
        """Start a shell process with a pseudo-terminal.

        Returns:
            Tuple of (master_fd, process).
        """
        master_fd, slave_fd = pty.openpty()

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"

        process = subprocess.Popen(
            [self._config.terminal_shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=env,
        )

        # Close slave in the parent process; the child owns it now
        os.close(slave_fd)

        logger.info(
            "Terminal: shell started (pid=%d, shell=%s)",
            process.pid,
            self._config.terminal_shell,
        )
        return master_fd, process

    async def _read_shell_output(self, master_fd: int):
        """Read output from the shell and send it over the WebSocket.

        Runs in an async loop, using a thread executor for the blocking
        os.read call. Uses an incremental UTF-8 decoder so that a
        multi-byte codepoint split across two os.read() chunks decodes
        correctly on the second chunk instead of becoming U+FFFD.
        """
        loop = asyncio.get_running_loop()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while not self._shutdown.is_set():
                try:
                    data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                except OSError:
                    # fd closed or process exited
                    break

                if not data:
                    # EOF: the decoder is flushed in the finally block
                    # below so a dangling incomplete sequence is reported
                    # (as U+FFFD) instead of silently swallowed. That
                    # same flush also runs on shutdown/exception paths
                    # that never hit this branch, so keep it in one place.
                    break

                text = decoder.decode(data)
                if not text:
                    # The decoder buffered an incomplete sequence; wait
                    # for the next chunk to complete it.
                    continue

                message = json.dumps({"type": "output", "data": text})

                try:
                    if self._ws is not None:
                        await self._ws.send(message)
                except websockets.exceptions.ConnectionClosed:
                    logger.debug("Terminal: WebSocket closed while sending output")
                    break
                except Exception as exc:
                    logger.error("Terminal: failed to send output: %s", exc)
                    break

        except Exception as exc:
            logger.error("Terminal: output reader error: %s", exc)
        finally:
            # Flush any buffered tail bytes the decoder is still holding
            # onto. This covers the EOF path (no-op if already flushed),
            # the shutdown path (loop exited via self._shutdown), and the
            # exception path (earlier raise) — in all three cases a
            # trailing incomplete UTF-8 sequence would otherwise be
            # silently dropped; flushing emits it as U+FFFD instead.
            try:
                tail = decoder.decode(b"", final=True)
            except Exception:
                tail = ""
            if tail and self._ws is not None:
                try:
                    await self._ws.send(json.dumps({"type": "output", "data": tail}))
                except Exception:
                    pass

            reason = "shell exited"
            if self._process is not None:
                retcode = self._process.poll()
                if retcode is not None:
                    reason = f"shell exited with code {retcode}"
            logger.info("Terminal: %s", reason)

            try:
                if self._ws is not None:
                    await self._ws.send(
                        json.dumps(
                            {
                                "type": "closed",
                                "reason": reason,
                            }
                        )
                    )
            except Exception:
                pass

    def _resize_pty(self, master_fd: int, cols: int, rows: int):
        """Resize the pseudo-terminal to the given dimensions."""
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            logger.debug("Terminal: resized to %dx%d", cols, rows)
        except Exception as exc:
            logger.warning("Terminal: resize failed: %s", exc)

    async def _handle_message(self, message: str):
        """Handle an incoming WebSocket message."""
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Terminal: received non-JSON message")
            return

        msg_type = msg.get("type", "")

        if msg_type == "input":
            data = msg.get("data", "")
            if self._master_fd is None or not data:
                return
            # JSON lets any scalar land in `data` (ints, booleans, None,
            # nested arrays). Coerce only strings and byte-likes; reject
            # the rest rather than letting os.write raise TypeError and
            # tearing down the session.
            if isinstance(data, str):
                raw = data.encode("utf-8")
            elif isinstance(data, (bytes, bytearray)):
                raw = bytes(data)
            else:
                logger.warning(
                    "Terminal: ignoring non-text input payload type=%s",
                    type(data).__name__,
                )
                return
            try:
                os.write(self._master_fd, raw)
            except OSError as exc:
                logger.error("Terminal: write to shell failed: %s", exc)

        elif msg_type == "resize":
            cols = max(1, min(msg.get("cols", 80), 500))
            rows = max(1, min(msg.get("rows", 24), 200))
            if self._master_fd is not None:
                self._resize_pty(self._master_fd, cols, rows)

        elif msg_type == "close":
            logger.info("Terminal: server requested close")
            await self._stop_shell()

        else:
            logger.debug("Terminal: unknown message type: %s", msg_type)

    async def _stop_shell(self):
        """Stop the running shell process and clean up."""
        if self._process is not None:
            pid = self._process.pid
            try:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
                logger.info("Terminal: shell process %d terminated", pid)
            except Exception as exc:
                logger.warning("Terminal: error stopping shell %d: %s", pid, exc)
            self._process = None

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    async def _connect_and_serve(self):
        """Connect to the WebSocket and handle the terminal session."""
        url = self._build_ws_url()
        logger.info("Terminal: connecting to server")
        logger.debug("Terminal: WebSocket URL: %s", url.split("?")[0])

        async with websockets.connect(
            url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            logger.info("Terminal: connected, waiting for commands")

            # Start the shell
            self._master_fd, self._process = self._start_shell()

            # Start the output reader as a background task
            reader_task = asyncio.create_task(self._read_shell_output(self._master_fd))

            try:
                async for message in ws:
                    if self._shutdown.is_set():
                        break
                    await self._handle_message(message)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.info("Terminal: WebSocket closed (code=%s)", exc.code)
            finally:
                reader_task.cancel()
                try:
                    await reader_task
                except asyncio.CancelledError:
                    pass
                await self._stop_shell()
                self._ws = None

    async def _run_async(self):
        """Main async loop with reconnection and exponential backoff."""
        backoff = BACKOFF_INITIAL

        while not self._shutdown.is_set():
            try:
                await self._connect_and_serve()
                # Successful connection resets backoff
                backoff = BACKOFF_INITIAL
            except websockets.exceptions.InvalidStatusCode as exc:
                logger.error(
                    "Terminal: server rejected connection (HTTP %s), retrying in %.0fs",
                    exc.status_code,
                    backoff,
                )
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                logger.warning(
                    "Terminal: connection error (%s), retrying in %.0fs",
                    exc,
                    backoff,
                )
            except Exception as exc:
                logger.error(
                    "Terminal: unexpected error (%s: %s), retrying in %.0fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )

            if self._shutdown.is_set():
                break

            # Wait with backoff, but check shutdown frequently
            wait_end = time.monotonic() + backoff
            while time.monotonic() < wait_end and not self._shutdown.is_set():
                await asyncio.sleep(0.5)

            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)

        logger.info("Terminal: client stopped")

    def run(self):
        """Run the terminal client (blocking). Meant to be called in a thread."""
        logger.info("Terminal: starting client")
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._run_async())
        except Exception as exc:
            logger.error("Terminal: event loop error: %s", exc)
        finally:
            self._loop.close()
            self._loop = None

    def stop(self):
        """Signal the terminal client to stop."""
        logger.info("Terminal: stop requested")
        self._shutdown.set()

        # Schedule shell cleanup on the event loop if it is running
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._stop_shell(), self._loop)
