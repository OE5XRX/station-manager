"""Shared, pure inventory serialization: the connect-time snapshot the browser
consumer sends AND the SSR page's initial state come from one builder (DRY)."""

from .models import StationModule


def snapshot(station):
    """Return the persisted inventory grouped by slot, in the same shape as the
    agent's live ``inventory`` frame. Includes a per-module ``online`` flag so
    offline modules still render from persisted descriptors + last_state."""
    slots, order = {}, []
    for m in StationModule.objects.filter(station=station):
        if m.slot not in slots:
            slots[m.slot] = []
            order.append(m.slot)
        slots[m.slot].append(
            {
                "module": m.module_id,
                "identity": {"type": m.type, "model": m.model, "version": m.version},
                "capabilities": m.capability_descriptor,
                "state": m.last_state,
                "online": m.online,
            }
        )
    return [{"slot": s, "modules": slots[s]} for s in order]
