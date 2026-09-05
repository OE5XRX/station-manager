"""Pure, synchronous audio-gate operations (analogue of apps/control/lock.py).

Every mutation is @transaction.atomic + row-locked so concurrent workers
can't race on the same station's gate row.  Callers (consumers) wrap these in
database_sync_to_async.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .constants import AUDIO_PTT_TTL
from .models import AudioGate  # noqa: E402

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_or_create(station) -> AudioGate:
    gate, _ = AudioGate.objects.get_or_create(station=station)
    return gate


def _locked(station) -> AudioGate:
    gate, _ = AudioGate.objects.select_for_update().get_or_create(station=station)
    return gate


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _coerce_slot(slot):
    """Coerce a slot value to an integer or None.

    The control-plane uses string slot ids like "slot0"; the audio gate stores
    the numeric slot number.  Accepts int, numeric str, "slotN" strings, or None.
    """
    if slot is None:
        return None
    if isinstance(slot, int):
        return slot
    s = str(slot)
    # "slot0" → 0, "slot1" → 1, etc.
    if s.startswith("slot"):
        try:
            return int(s[4:])
        except ValueError:
            pass
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


@transaction.atomic
def set_ptt(station, slot, module, ttl: float | None = None) -> None:
    """Activate PTT for slot/module with a dead-man expiry."""
    if ttl is None:
        ttl = AUDIO_PTT_TTL
    gate = _locked(station)
    gate.ptt_active = True
    gate.ptt_slot = _coerce_slot(slot)
    gate.ptt_module = module or ""
    gate.ptt_expires_at = timezone.now() + timedelta(seconds=ttl)
    gate.save(update_fields=["ptt_active", "ptt_slot", "ptt_module", "ptt_expires_at"])


@transaction.atomic
def refresh_ptt(station, ttl: float | None = None) -> None:
    """Extend PTT dead-man expiry (called on each keepalive tick)."""
    if ttl is None:
        ttl = AUDIO_PTT_TTL
    gate = _locked(station)
    if gate.ptt_active:
        gate.ptt_expires_at = timezone.now() + timedelta(seconds=ttl)
        gate.save(update_fields=["ptt_expires_at"])


@transaction.atomic
def clear_ptt(station) -> None:
    """Deactivate PTT regardless of who holds it (called on lock loss)."""
    gate = _locked(station)
    gate.ptt_active = False
    gate.ptt_slot = None
    gate.ptt_module = ""
    gate.ptt_expires_at = None
    gate.save(update_fields=["ptt_active", "ptt_slot", "ptt_module", "ptt_expires_at"])


@transaction.atomic
def set_tx_route(station, slot, module) -> None:
    """Record which module the operator mic transmits into."""
    gate = _locked(station)
    gate.tx_slot = _coerce_slot(slot)
    gate.tx_module = module or ""
    gate.save(update_fields=["tx_slot", "tx_module"])


@transaction.atomic
def clear_tx_route(station) -> None:
    """Clear the TX route (tx_route command with value=null)."""
    gate = _locked(station)
    gate.tx_slot = None
    gate.tx_module = ""
    gate.save(update_fields=["tx_slot", "tx_module"])


def get_state(station) -> dict:
    """Return a plain dict snapshot of the gate (no lock required — read-only).

    Safe defaults if no row exists yet.  NOTE: this dict carries a raw
    ``datetime`` in ``ptt_expires_at`` and MUST NOT be used as a channel-layer
    broadcast payload (msgpack can't serialize datetime — see get_wire_state).
    """
    try:
        gate = AudioGate.objects.get(station=station)
    except AudioGate.DoesNotExist:
        return {
            "ptt_active": False,
            "ptt_slot": None,
            "ptt_module": "",
            "ptt_expires_at": None,
            "tx_slot": None,
            "tx_module": "",
        }
    return {
        "ptt_active": gate.ptt_active,
        "ptt_slot": gate.ptt_slot,
        "ptt_module": gate.ptt_module,
        "ptt_expires_at": gate.ptt_expires_at,
        "tx_slot": gate.tx_slot,
        "tx_module": gate.tx_module,
    }


def get_wire_state(station) -> dict:
    """Return an msgpack-safe gate snapshot for channel-layer broadcasts.

    Contains ONLY primitives — no ``datetime`` — so it round-trips through the
    prod ``channels_redis`` (msgpack) layer.  ``ptt_expires_at`` is exposed as
    ``ptt_expires_epoch`` (Unix timestamp float, or None).  Adds ``holder_id``
    (the current ControlLock holder) so the browser can decide the uplink gate
    purely in memory without a per-frame DB query (design §7).
    """
    # Lazy import to break the audio <-> control import cycle.
    from apps.control.models import ControlLock

    holder_id = (
        ControlLock.objects.filter(station=station, scope="station")
        .values_list("holder_id", flat=True)
        .first()
    )

    try:
        gate = AudioGate.objects.get(station=station)
    except AudioGate.DoesNotExist:
        return {
            "ptt_active": False,
            "ptt_slot": None,
            "ptt_module": "",
            "ptt_expires_epoch": None,
            "tx_slot": None,
            "tx_module": "",
            "holder_id": holder_id,
        }
    return {
        "ptt_active": gate.ptt_active,
        "ptt_slot": gate.ptt_slot,
        "ptt_module": gate.ptt_module,
        "ptt_expires_epoch": (
            gate.ptt_expires_at.timestamp() if gate.ptt_expires_at is not None else None
        ),
        "tx_slot": gate.tx_slot,
        "tx_module": gate.tx_module,
        "holder_id": holder_id,
    }


def mic_allowed(station, user) -> bool:
    """True iff user holds the ControlLock AND PTT is active AND not expired.

    Lazy import of ControlLock to avoid an app import cycle between
    apps.audio and apps.control.
    """
    # Lazy import to break the audio <-> control import cycle.
    from apps.control.models import ControlLock

    try:
        lock = ControlLock.objects.select_related("holder").get(
            station=station, scope="station"
        )
    except ControlLock.DoesNotExist:
        return False
    if lock.holder_id != user.id:
        return False
    try:
        gate = AudioGate.objects.get(station=station)
    except AudioGate.DoesNotExist:
        return False
    if not gate.ptt_active:
        return False
    if gate.ptt_expires_at is None:
        return False
    if timezone.now() >= gate.ptt_expires_at:
        return False
    return True
