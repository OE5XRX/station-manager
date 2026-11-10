"""systemd template-unit control for decoders (``oe5xrx-decoder@<decoder>-slot<N>``).

All calls block; callers run them in an executor. The unit's wrapper reads its
parameters from ``<env_dir>/<instance>.env`` (``EnvironmentFile=``).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger(__name__)

UNIT_TEMPLATE = "oe5xrx-decoder@{instance}.service"
ENV_DIR = "/run/oe5xrx/decoder"

_DECODER_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
_ENV_KEY_RE = re.compile(r"^OE5XRX_[A-Z_]+$")
_ENV_VAL_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")


class DecoderUnitError(Exception):
    pass


class DecoderUnits:
    def __init__(self, *, run=subprocess.run, env_dir: str = ENV_DIR, timeout_s: float = 15.0):
        self._run = run
        self._env_dir = env_dir
        self._timeout = timeout_s

    def instance(self, decoder: str, slot: int) -> str:
        if not isinstance(decoder, str) or not _DECODER_RE.fullmatch(decoder):
            raise DecoderUnitError("bad decoder name")
        return f"{decoder}-slot{int(slot)}"

    def _unit(self, decoder: str, slot: int) -> str:
        return UNIT_TEMPLATE.format(instance=self.instance(decoder, slot))

    def _env_path(self, decoder: str, slot: int) -> str:
        return os.path.join(self._env_dir, f"{self.instance(decoder, slot)}.env")

    def _systemctl(self, *args: str):
        try:
            return self._run(
                ["systemctl", *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DecoderUnitError(f"systemctl {args[0]}: {type(exc).__name__}") from exc

    def start(self, decoder: str, slot: int, env: dict[str, str]) -> None:
        unit = self._unit(decoder, slot)
        for key, val in env.items():
            if not _ENV_KEY_RE.fullmatch(str(key)) or not _ENV_VAL_RE.fullmatch(str(val)):
                raise DecoderUnitError("bad env")
        self._write_env(self._env_path(decoder, slot), env)
        res = self._systemctl("start", unit)
        if res.returncode != 0:
            raise DecoderUnitError((res.stderr or "").strip()[:200] or "start failed")

    def _write_env(self, path: str, env: dict[str, str]) -> None:
        try:
            os.makedirs(self._env_dir, mode=0o755, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._env_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write("".join(f"{k}={v}\n" for k, v in env.items()))
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise DecoderUnitError(f"env write: {type(exc).__name__}") from exc

    def stop(self, decoder: str, slot: int) -> None:
        unit = self._unit(decoder, slot)
        try:
            res = self._systemctl("stop", unit)
        finally:
            self._remove(self._env_path(decoder, slot))
        if res.returncode != 0:
            raise DecoderUnitError((res.stderr or "").strip()[:200] or "stop failed")

    def state(self, decoder: str, slot: int) -> str:
        try:
            res = self._systemctl("is-active", self._unit(decoder, slot))
        except DecoderUnitError:
            return "unknown"
        return (res.stdout or "").strip() or "unknown"

    def stop_all(self) -> None:
        try:
            self._systemctl("stop", UNIT_TEMPLATE.format(instance="*"))
        except DecoderUnitError:
            logger.warning("stopping leftover decoder units failed", exc_info=True)
        try:
            names = os.listdir(self._env_dir)
        except OSError:
            return
        for name in names:
            if name.endswith(".env"):
                self._remove(os.path.join(self._env_dir, name))

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass
