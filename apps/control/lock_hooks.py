"""One hook pair that every control-lock path goes through.

``on_lock_released`` / ``on_lock_acquired`` run the built-in side effects of a lock change
(scan stop, PTT/TX-route clear, ``audio.gate`` broadcast) and then the registered hooks
(e.g. the passive-services supervisor). Audit rows and the ``control.lock`` broadcast stay
at the call sites: their messages are user-specific.

``reason`` values: ``released``, ``revoked``, ``expired``, ``agent_disconnect`` (release);
``acquired``, ``transferred``, ``preempted`` (acquire).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer

from . import scan

logger = logging.getLogger(__name__)

# A registered hook runs inline in the lock path (and the sweep loop): bound it.
HOOK_TIMEOUT_S = 10

# Scan-stop reason texts (kept as they were when each path inlined them).
_SCAN_STOP_TEXT = {
    "released": "lock released",
    "revoked": "lock released",
    "expired": "lock expired",
    "transferred": "lock transferred",
    "preempted": "lock preempted",
}

_release_hooks: list[Callable[..., Awaitable[None]]] = []
_acquire_hooks: list[Callable[..., Awaitable[None]]] = []


def register_release_hook(fn: Callable[..., Awaitable[None]]) -> None:
    """``fn(station, reason)``; runs after the built-in release sequence."""
    if fn not in _release_hooks:
        _release_hooks.append(fn)


def register_acquire_hook(fn: Callable[..., Awaitable[None]]) -> None:
    """``fn(station, holder, reason)``; runs after the built-in acquire sequence."""
    if fn not in _acquire_hooks:
        _acquire_hooks.append(fn)


def unregister_hooks(*fns) -> None:
    """Remove hooks again (tests)."""
    for fn in fns:
        for registry in (_release_hooks, _acquire_hooks):
            if fn in registry:
                registry.remove(fn)


async def on_lock_released(
    station, reason: str, *, stop_scan: bool = True, clear_tx_route: bool = False
) -> None:
    """The lock became free. ``stop_scan=False``: the agent is gone, the caller already
    dropped the scan row. ``clear_tx_route``: the holder lost ``control.use``."""
    if stop_scan:
        await _stop_scan(station, _SCAN_STOP_TEXT.get(reason, f"lock {reason}"))
    await database_sync_to_async(_clear_gate)(station, clear_tx_route)
    await _broadcast_gate(station)
    for hook in tuple(_release_hooks):
        try:
            await asyncio.wait_for(hook(station, reason), HOOK_TIMEOUT_S)
        except Exception:
            logger.exception("control: release hook %r failed (reason=%s)", hook, reason)


async def on_lock_acquired(station, holder, reason: str, *, previous_holder_lost: bool) -> None:
    """``holder`` now holds the lock. ``previous_holder_lost``: somebody else held it
    (transfer/preempt), so their scan and PTT must die; a plain grant runs no built-ins."""
    if previous_holder_lost:
        await _stop_scan(
            station,
            _SCAN_STOP_TEXT.get(reason, f"lock {reason}"),
            unless_user=holder if reason == "preempted" else None,
        )
        await database_sync_to_async(_clear_gate)(station, False)
        await _broadcast_gate(station)
    for hook in tuple(_acquire_hooks):
        try:
            await asyncio.wait_for(hook(station, holder, reason), HOOK_TIMEOUT_S)
        except Exception:
            logger.exception("control: acquire hook %r failed (reason=%s)", hook, reason)


async def _stop_scan(station, reason_text, unless_user=None):
    try:
        await database_sync_to_async(scan.stop_active_scan)(
            station, reason_text, unless_user=unless_user
        )
    except Exception:
        logger.exception("control: stopping scan failed")


def _clear_gate(station, clear_tx_route):
    # Lazy import: the audio app imports from control.
    from apps.audio import gate

    gate.clear_ptt(station)
    if clear_tx_route:
        gate.clear_tx_route(station)


async def _broadcast_gate(station):
    """``audio.gate`` to the browser and agent audio groups (msgpack-safe wire state)."""
    from apps.audio import constants as audio_constants
    from apps.audio import gate

    state = await database_sync_to_async(gate.get_wire_state)(station)
    payload = {"type": "audio.gate", "state": state}
    layer = get_channel_layer()
    await layer.group_send(audio_constants.browser_group(station.pk), payload)
    await layer.group_send(audio_constants.agent_group(station.pk), payload)
