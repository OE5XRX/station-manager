"""Station-side SSTV transmission: download → key → play → unkey (SSTV spec §5.4).

The server synthesises the SSTV audio (8 kHz mono s16le) and sends
``sstv_tx_start{request_id, tx_id, slot, module, url, sha256, size, duration_s}``. The agent
downloads it over the DeviceKey-signed HTTP client (size + sha256 checked before keying), then
owns the PTT for the whole transmission: it keys via :meth:`Broker.key_session` (no browser
keepalive dead-man), plays the file straight into the slot's TX node at the calibrated TX
ceiling (never through the voice DSP chain) and unkeys.

The transmitter must never stay keyed. Every way out of :meth:`SstvTxSession.run` unkeys:
playback end, the hard stop at ``duration_s + HARD_STOP_MARGIN_S`` (independent of the
server), ``sstv_tx_stop``, control-WS loss, a player crash, agent shutdown and a voice unkey.
A ``tx_id`` is accepted only once per agent process, so a reconnecting server that replays an
old ``sstv_tx_start`` never re-keys.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import math
import os
import re
import tempfile
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from station_agent import protocol as proto
from station_agent.audio.tx_settings import clamp_ceiling

logger = logging.getLogger(__name__)

# Longest mode (Scottie DX, ~269 s) at 16 KB/s is ~4.3 MB.
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_DURATION_S = 600.0
HARD_STOP_MARGIN_S = 5.0
SAMPLE_RATE = 8000
RUNTIME_DIR = "/run/oe5xrx/sstv-tx"
# The voice chain's limiter peaks at 0.9 of the ceiling; SSTV tones are full scale.
_VOICE_PEAK = 0.9
_DOWNLOAD_TIMEOUT_S = 30
_CHUNK = 64 * 1024
_KILL_WAIT_S = 2.0
_SEEN_IDS = 256

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_TX_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

BAD_REQUEST = "bad_request"
SSTV_BUSY = "sstv_busy"
SLOT_BUSY = "slot_busy"


def tx_gain(ceiling_dbfs) -> float:
    """Linear gain that puts full-scale SSTV tones at the station's TX ceiling."""
    return 10 ** (clamp_ceiling(ceiling_dbfs) / 20) / _VOICE_PEAK


def build_play_argv(path: str, tx_node: str, gain: float) -> list[str]:
    return [
        "gst-launch-1.0",
        "filesrc",
        f"location={path}",
        "!",
        "rawaudioparse",
        "format=pcm",
        "pcm-format=s16le",
        f"sample-rate={SAMPLE_RATE}",
        "num-channels=1",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        "volume",
        f"volume={gain:.6f}",
        "!",
        "pipewiresink",
        f"target-object={tx_node}",
    ]


async def _spawn_player(argv: list[str]):
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


class _SessionEndError(Exception):
    def __init__(self, state: str, detail: str):
        super().__init__(detail)
        self.state, self.detail = state, detail


def _int_field(value, lo, hi) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


class SstvTxSession:
    def __init__(self, manager: SstvTxManager, radio, send, frame: dict):
        self._mgr = manager
        self._radio = radio
        self._send = send
        self.tx_id: str = frame["tx_id"]
        self.slot: int = frame["slot"]
        self.module: str = frame["module"]
        self._url_path: str = frame["path"]
        self._sha256: str = frame["sha256"]
        self._size: int = frame["size"]
        self.duration_s: float = float(frame["duration_s"])
        self._stopped = asyncio.Event()
        self._reason = "stopped"
        # Tells the download thread to give up (it cannot be cancelled from the loop).
        self._cancel_download = threading.Event()
        self.task: asyncio.Task | None = None

    def abort(self, reason: str) -> None:
        """Request the end of the session. The first reason wins; run() does the unkey."""
        if not self._stopped.is_set():
            self._reason = reason
            self._stopped.set()
        self._cancel_download.set()

    async def _emit(self, state: str, progress: float, detail: str | None = None) -> None:
        try:
            await self._send(proto.build_sstv_tx_state(self.tx_id, state, progress, detail))
        except Exception:  # noqa: BLE001 — a dead link must never skip the unkey
            logger.exception("sstv_tx: sending state %s failed", state)

    async def _until_stopped(self, aw: Awaitable, timeout: float | None = None):
        """Await *aw* unless the session is stopped first (or *timeout* passes).

        Returns ``(True, result)`` when *aw* finished, ``(False, None)`` otherwise. The
        awaitable is left running if it did not finish (the caller owns its cleanup).
        """
        fut = asyncio.ensure_future(aw)
        stop = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait({fut, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if fut.done():
            return True, fut.result()
        return False, None

    def _check_stopped(self) -> None:
        if self._stopped.is_set():
            raise _SessionEndError("aborted", self._reason)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        state, detail, progress = "error", "internal", 0.0
        fd, tmp = None, None
        keyed = False
        proc = None
        try:
            await self._emit("downloading", 0.0)
            os.makedirs(self._mgr.runtime_dir, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="tx-", suffix=".pcm", dir=self._mgr.runtime_dir)
            dl_fd, fd = fd, None  # the download thread owns (and closes) the descriptor
            _, err = await self._until_stopped(
                loop.run_in_executor(self._mgr.executor, self._download, dl_fd)
            )
            self._check_stopped()
            if err:
                raise _SessionEndError("error", err)
            _, node = await self._until_stopped(
                loop.run_in_executor(self._mgr.executor, self._mgr.tx_node, self.slot)
            )
            self._check_stopped()
            if not node:
                raise _SessionEndError("error", "no_tx_node")
            gain = tx_gain(self._mgr.ceiling_dbfs())
            # Mark the session keyed BEFORE the key command: if it fails mid-flight (timeout)
            # the key may still have landed, so the finally below must drive the PTT low.
            keyed = True
            err = await self._radio.key_session(self.slot, self.module)
            if err:
                raise _SessionEndError("error", err)
            keyed_at = loop.time()
            self._check_stopped()
            await self._emit("keyed", 0.0)
            try:
                proc = await self._mgr.spawn(build_play_argv(tmp, node, gain))
            except Exception:  # noqa: BLE001 — missing/broken player
                logger.exception("sstv_tx: starting the player failed")
                raise _SessionEndError("error", "player_failed")
            state, detail, progress = await self._play(proc, keyed_at)
        except _SessionEndError as fail:
            state, detail = fail.state, fail.detail
        except asyncio.CancelledError:
            state, detail = "aborted", "cancelled"
            raise
        except Exception:  # noqa: BLE001 — anything unexpected still ends low
            logger.exception("sstv_tx: session %s failed", self.tx_id)
            state, detail = "error", "internal"
        finally:
            await self._cleanup(proc, keyed, fd, tmp)
            await self._emit(state, progress, detail)
            logger.info("sstv_tx: %s ended %s (%s)", self.tx_id, state, detail)

    async def _play(self, proc, keyed_at: float):
        loop = asyncio.get_running_loop()
        deadline = keyed_at + self._mgr.hard_stop_after(self.duration_s)
        wait = asyncio.ensure_future(proc.wait())
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return "aborted", "hard_stop", self._progress(keyed_at)
                finished, rc = await self._until_stopped(
                    wait, timeout=min(remaining, self._mgr.progress_interval_s)
                )
                if finished:
                    if rc == 0:
                        return "done", None, 1.0
                    return "error", f"player_failed rc={rc}", self._progress(keyed_at)
                self._check_stopped()
                if loop.time() < deadline:
                    await self._emit("keyed", self._progress(keyed_at))
        finally:
            if not wait.done():
                wait.cancel()

    def _progress(self, keyed_at: float) -> float:
        elapsed = asyncio.get_running_loop().time() - keyed_at
        return max(0.0, min(1.0, elapsed / self.duration_s))

    async def _cleanup(self, proc, keyed: bool, fd, tmp) -> None:
        # Order matters: stop the audio, drive the transmitter low, then tidy up.
        try:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            if keyed:
                try:
                    await asyncio.shield(
                        self._radio.unkey_session(self.slot, self.module, self._reason)
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("sstv_tx: unkey failed for %s", self.tx_id)
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), _KILL_WAIT_S)
                except Exception:  # noqa: BLE001 — TimeoutError or a broken process handle
                    logger.warning("sstv_tx: player for %s did not exit", self.tx_id)
        finally:
            if fd is not None:
                os.close(fd)
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass

    def _download(self, fd: int) -> str | None:
        """Blocking: stream the artefact into *fd*. Returns an error detail or None."""
        with os.fdopen(fd, "wb") as out:
            if self._cancel_download.is_set():
                return "cancelled"
            resp = self._mgr.http_get(self._url_path)
            if resp is None:
                return "download_failed"
            try:
                if resp.status_code != 200:
                    return f"http_{resp.status_code}"
                length = resp.headers.get("Content-Length")
                if length is not None and length.strip() != str(self._size):
                    return "size_mismatch"
                digest, total = hashlib.sha256(), 0
                for chunk in resp.iter_content(_CHUNK):
                    if self._cancel_download.is_set():
                        return "cancelled"
                    total += len(chunk)
                    if total > self._size:
                        return "size_mismatch"
                    digest.update(chunk)
                    out.write(chunk)
                if total != self._size:
                    return "size_mismatch"
                if digest.hexdigest() != self._sha256:
                    return "checksum_mismatch"
                return None
            except Exception:  # noqa: BLE001 — requests/IO errors mid-stream
                logger.exception("sstv_tx: download of %s failed", self.tx_id)
                return "download_failed"
            finally:
                resp.close()


class SstvTxManager:
    """Process-lifetime owner of the (at most one) SSTV TX session.

    Shared by every connection's Broker, so the set of already-seen ``tx_id`` survives a
    control-WS reconnect.
    """

    def __init__(
        self,
        *,
        http_get: Callable[[str], object],
        server_url: str,
        ceiling_dbfs: Callable[[], float],
        tx_node: Callable[[int], str | None],
        runtime_dir: str = RUNTIME_DIR,
        spawn: Callable[[list[str]], Awaitable] = _spawn_player,
        hard_stop_margin_s: float = HARD_STOP_MARGIN_S,
        progress_interval_s: float = 1.0,
    ):
        self._http_get = http_get
        self._server_url = server_url.rstrip("/")
        self.ceiling_dbfs = ceiling_dbfs
        self.tx_node = tx_node
        self.runtime_dir = runtime_dir
        self._spawn = spawn
        self._margin = hard_stop_margin_s
        self.progress_interval_s = progress_interval_s
        # Own pool: a stalled download must not tie up the default executor that serves
        # serial I/O (commands, PTT, telemetry). Two workers so a download thread still
        # draining after an abort never blocks the next session.
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sstv-tx")
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._session: SstvTxSession | None = None
        self._closed = False

    # --- helpers ------------------------------------------------------------
    def hard_stop_after(self, duration_s: float) -> float:
        return duration_s + self._margin

    async def spawn(self, argv: list[str]):
        return await self._spawn(argv)

    def http_get(self, path: str):
        return self._http_get(path)

    def _url_path(self, url) -> str | None:
        """Server-relative path of *url*, or None if it points anywhere but our server.

        The signed client always talks to the configured server; a foreign URL is refused
        rather than silently re-targeted."""
        if not isinstance(url, str) or not url or any(c in url for c in "\r\n\\ "):
            return None
        if url.startswith("/") and not url.startswith("//"):
            path = url
        elif url.startswith(self._server_url + "/"):
            path = url[len(self._server_url) :]
        else:
            return None
        parts = urlsplit(path)
        if parts.scheme or parts.netloc:
            return None
        return path

    def _validate(self, msg: dict) -> dict | None:
        tx_id, slot, module = msg.get("tx_id"), msg.get("slot"), msg.get("module")
        sha, size, duration = msg.get("sha256"), msg.get("size"), msg.get("duration_s")
        if not isinstance(tx_id, str) or not _TX_ID_RE.fullmatch(tx_id):
            return None
        if not _int_field(slot, 0, 1_000_000) or not isinstance(module, str) or not module:
            return None
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            return None
        if not _int_field(size, 1, MAX_AUDIO_BYTES):
            return None
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            return None
        try:
            if not (math.isfinite(duration) and 0 < duration <= MAX_DURATION_S):
                return None
        except (OverflowError, ValueError):
            return None
        path = self._url_path(msg.get("url"))
        if path is None:
            return None
        return {
            "tx_id": tx_id,
            "slot": slot,
            "module": module,
            "sha256": sha,
            "size": size,
            "duration_s": float(duration),
            "path": path,
        }

    def _remember(self, tx_id: str) -> None:
        self._seen[tx_id] = None
        while len(self._seen) > _SEEN_IDS:
            self._seen.popitem(last=False)

    @property
    def active(self) -> SstvTxSession | None:
        s = self._session
        return s if s is not None and s.task is not None and not s.task.done() else None

    def owns(self, slot, module=None) -> bool:
        s = self.active
        return s is not None and s.slot == slot and (module is None or s.module == module)

    # --- frames -------------------------------------------------------------
    async def handle_start(self, radio, send, msg: dict) -> None:
        rid = msg.get("request_id")

        async def refuse(code: str, text: str = "") -> None:
            await send(proto.build_result(rid, False, error=(code, text)))

        if self._closed:
            await refuse("unsupported", "shutting down")
            return
        frame = self._validate(msg)
        if frame is None:
            await refuse(BAD_REQUEST, "sstv_tx_start")
            return
        if frame["tx_id"] in self._seen:
            # Replayed after a reconnect (or a duplicate): a finished/aborted TX never re-keys.
            await refuse(BAD_REQUEST, "tx_id already used")
            return
        if self.active is not None:
            await refuse(SSTV_BUSY)
            return
        if radio.slot_busy(frame["slot"]):
            await refuse(SLOT_BUSY)
            return
        self._remember(frame["tx_id"])
        # Ack first, so the server sees the result before the first sstv_tx_state. Frames are
        # handled one at a time, so nothing can start a second session in between.
        await send(proto.build_result(rid, True, value={"tx_id": frame["tx_id"]}))
        session = SstvTxSession(self, radio, send, frame)
        self._session = session
        session.task = asyncio.get_running_loop().create_task(session.run())

    async def handle_stop(self, msg: dict) -> None:
        s = self.active
        if s is not None and msg.get("tx_id") == s.tx_id:
            s.abort("stopped")

    async def abort(self, reason: str, slot=None) -> None:
        """Abort the running session (optionally only if it is on *slot*) and wait until the
        transmitter is low."""
        s = self.active
        if s is None or (slot is not None and s.slot != slot):
            return
        s.abort(reason)
        await asyncio.gather(s.task, return_exceptions=True)

    async def wait_idle(self) -> None:
        s = self._session
        if s is not None and s.task is not None:
            await asyncio.gather(s.task, return_exceptions=True)

    async def shutdown(self) -> None:
        self._closed = True
        await self.abort("shutdown")
        self.executor.shutdown(wait=False, cancel_futures=True)
