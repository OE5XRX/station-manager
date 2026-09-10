"""Runtime audio-path detection (interim, decoupled from firmware `describe`).

The agent enables audio when a discovered slot actually has an audio path, independent of
any config flag: `PipeWireRouterBackend.list_audio_slots()` enumerates ALSA cards tagged
`OE5XRX_SLOT` via udev. A non-empty list means at least one slot exposes audio hardware.

TODO(FW-describe): once the parallel FW session ships the `audio` capability in
`module describe`, switch this to gate on that capability from the discovered inventory
instead of probing ALSA/udev directly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def audio_path_present(config, backend=None) -> bool:
    """True if any slot exposes an audio path. Fail-closed: any error → False (audio off)."""
    if backend is None:
        from station_agent.audio.router_backend import PipeWireRouterBackend

        backend = PipeWireRouterBackend(
            sysfs_sound=getattr(config, "audio_sysfs_sound", "/sys/class/sound")
        )
    try:
        slots = backend.list_audio_slots()
    except Exception:  # noqa: BLE001 — detection must never crash agent startup
        logger.debug("audio detect: backend.list_audio_slots() failed", exc_info=True)
        return False
    present = bool(slots)
    logger.info("audio detect: audio path %s (slots=%s)", "present" if present else "absent", slots)
    return present
