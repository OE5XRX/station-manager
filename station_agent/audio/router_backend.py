"""RouterBackend — slot↔PipeWire-node mapping and graph ops, behind an interface.

Two responsibilities, both isolated so a future libpipewire backend can replace this one:

1. **slot → PipeWire node** (Spec 0 §12 Finding 2). The Session-A WirePlumber rule that
   tried to rename real-HW nodes by USB port is confirmed broken: the real ``node.name``
   carries the module *serial*, not the port. So resolution keys on the ``OE5XRX_SLOT`` udev
   tag → ALSA card index → the PipeWire node whose ``api.alsa.card`` matches, and returns
   whatever ``node.name`` that node has (serial-based on real HW, ``oe5xrx.slotN`` in sim).
   One mechanism for control and audio; sim and real resolve identically.
2. **graph ops** — link/unlink via ``pw-link``, volume via ``wpctl``.

Everything shells out through an injected ``run`` callable, so unit tests never need
PipeWire, WirePlumber, or a real sysfs. Nothing here raises into the caller: a failed
resolve returns ``None``, a failed op returns ``False`` (mirrors the slot_discovery/
slot_control fail-closed hygiene).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

_DIRECTION_CLASS = {"rx": "Audio/Source", "tx": "Audio/Sink"}
_SLOT_RE = re.compile(r"^OE5XRX_SLOT=(\d+)\s*$", re.MULTILINE)
_CARD_RE = re.compile(r"card(\d+)$")

_DEFAULT_TIMEOUT = 5.0


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


def _default_run(argv: list[str], timeout: float = _DEFAULT_TIMEOUT) -> RunResult:
    """Run a subprocess, capturing text output. Raises OSError/subprocess errors up to the
    backend method, which converts them to a fail-closed None/False."""
    proc = subprocess.run(  # noqa: S603 — argv is built from fixed tool names + resolved ids
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


class RouterBackend(Protocol):
    def resolve_node(self, slot: int, direction: str) -> str | None: ...
    def alsa_card_for_slot(self, slot: int) -> int | None: ...
    def list_audio_slots(self) -> list[int]: ...
    def link(self, out_node: str, in_node: str) -> bool: ...
    def unlink(self, out_node: str, in_node: str) -> bool: ...
    def set_volume(self, node: str, linear: float) -> bool: ...


class PipeWireRouterBackend:
    def __init__(
        self,
        run: Callable[..., RunResult] = _default_run,
        sysfs_sound: str = "/sys/class/sound",
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._run = run
        self._sysfs_sound = sysfs_sound
        self._timeout = timeout

    # --- slot → node -------------------------------------------------------
    def resolve_node(self, slot: int, direction: str) -> str | None:
        if direction not in _DIRECTION_CLASS:
            raise ValueError(f"direction must be 'rx' or 'tx', got {direction!r}")
        card = self._card_for_slot(slot)
        if card is None:
            logger.debug("router: no ALSA card tagged OE5XRX_SLOT=%s", slot)
            return None
        want_class = _DIRECTION_CLASS[direction]
        for node in self._pw_nodes():
            props = node.get("info", {}).get("props", {})
            if props.get("api.alsa.card") == card and props.get("media.class") == want_class:
                name = props.get("node.name")
                if isinstance(name, str) and name:
                    return name
        logger.debug("router: no %s node for slot %s (card %s)", want_class, slot, card)
        return None

    def alsa_card_for_slot(self, slot: int) -> int | None:
        """Public ALSA card index for ``slot`` (via the ``OE5XRX_SLOT`` udev tag).

        Same resolution as :meth:`resolve_node`, exposed for callers that need the raw ALSA
        device rather than the PipeWire node — e.g. the ``selftest audio`` TX check taps the sim
        reverse cable on the raw ``hw:<card>,0,0`` dev0 capture (Spec 0 §8), which WirePlumber
        intentionally does not expose as a PipeWire node.
        """
        return self._card_for_slot(slot)

    def list_audio_slots(self) -> list[int]:
        slots = set()
        for card in self._card_indices():
            slot = self._slot_for_card(card)
            if slot is not None:
                slots.add(slot)
        return sorted(slots)

    def _card_indices(self) -> list[int]:
        out = []
        for path in glob.glob(os.path.join(self._sysfs_sound, "card*")):
            m = _CARD_RE.search(os.path.basename(path))
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def _card_for_slot(self, slot: int) -> int | None:
        for card in self._card_indices():
            if self._slot_for_card(card) == slot:
                return card
        return None

    def _slot_for_card(self, card: int) -> int | None:
        """OE5XRX_SLOT udev property for an ALSA card, via ``udevadm info`` (no pyudev).

        udev ENV properties live in the udev database, not the kernel sysfs uevent, so we
        must ask udevadm rather than read the file directly.
        """
        path = os.path.join(self._sysfs_sound, f"card{card}")
        res = self._safe_run(["udevadm", "info", "--query=property", "--path", path])
        if res is None or res.returncode != 0:
            return None
        m = _SLOT_RE.search(res.stdout)
        return int(m.group(1)) if m else None

    def _pw_nodes(self) -> list[dict]:
        res = self._safe_run(["pw-dump"])
        if res is None or res.returncode != 0:
            return []
        try:
            data = json.loads(res.stdout)
        except (json.JSONDecodeError, ValueError):
            logger.debug("router: pw-dump did not return valid JSON")
            return []
        if not isinstance(data, list):
            return []
        return [o for o in data if o.get("type") == "PipeWire:Interface:Node"]

    # --- graph ops ---------------------------------------------------------
    def link(self, out_node: str, in_node: str) -> bool:
        # pw-link connects all matching ports of two nodes when given node names.
        return self._ok(self._safe_run(["pw-link", out_node, in_node]))

    def unlink(self, out_node: str, in_node: str) -> bool:
        return self._ok(self._safe_run(["pw-link", "--disconnect", out_node, in_node]))

    def set_volume(self, node: str, linear: float) -> bool:
        return self._ok(self._safe_run(["wpctl", "set-volume", node, f"{linear:g}"]))

    # --- helpers -----------------------------------------------------------
    def _safe_run(self, argv: list[str]) -> RunResult | None:
        try:
            return self._run(argv, timeout=self._timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("router: subprocess %s failed: %s", argv[0], exc)
            return None

    @staticmethod
    def _ok(res: RunResult | None) -> bool:
        return res is not None and res.returncode == 0
