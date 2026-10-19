# apps/control/lock.py
"""Pure, synchronous TX-lock operations over ControlLock rows.

Every mutation is atomic + row-locked so concurrent workers can't both
acquire. Callers (consumers) wrap these in database_sync_to_async and
broadcast lock_status() afterwards.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.accounts.permissions import can

from .models import ControlLock

# Refusal text for new control grants while a module firmware update drains the
# station (Teilbereich D). Consumers send it with code "firmware_update_pending".
FIRMWARE_DRAIN_MESSAGE = (
    "Firmware-Update ausstehend — neue Steuerung ist gesperrt, bis das Update abgeschlossen ist."
)


class _DrainRefused:
    """Falsy refusal marker: the grant was refused BECAUSE the station drains.

    Callers that only test truthiness keep working; consumers compare with ``is``
    to send ``firmware_update_pending`` only for a drain refusal (not when, say,
    another user simply holds the lock)."""

    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):
        return "DRAIN_REFUSED"


DRAIN_REFUSED = _DrainRefused()


def get_or_create_lock(station, scope="station"):
    lock, _ = ControlLock.objects.get_or_create(station=station, scope=scope)
    return lock


def _locked(station, scope):
    return ControlLock.objects.select_for_update().get_or_create(station=station, scope=scope)[0]


def _fresh_station(station):
    """Re-read the Station row: consumers pass a connection-cached instance whose
    ``last_seen`` / ``agent_capabilities`` (both drain inputs) may be stale."""
    from apps.stations.models import Station

    return Station.objects.filter(pk=station.pk).first()


def is_draining(station):
    """Whether new control grants are refused because a module firmware update
    is pending. Lazy import: module_firmware must not be a control import-time
    dependency (cycle)."""
    from apps.module_firmware import drain

    fresh = _fresh_station(station)
    return fresh is not None and drain.is_draining(fresh)


def drain_payload(station):
    """``{"since": iso8601}`` while draining, else ``None`` (lock frame field)."""
    from apps.module_firmware import drain

    fresh = _fresh_station(station)
    if fresh is None:
        return None
    status = drain.drain_status(fresh)
    if not status.active:
        return None
    return {"since": status.since.isoformat() if status.since else None}


def acquire(station, user, scope="station"):
    """True on grant, ``DRAIN_REFUSED`` for a free lock while draining, else False.

    The drain is evaluated BEFORE the row lock (it runs several queries) so the
    ControlLock row is never held across it."""
    return _acquire(station, user, scope, is_draining(station))


@transaction.atomic
def _acquire(station, user, scope, draining):
    lock = _locked(station, scope)
    if lock.holder_id == user.id or lock.holder_id is None:
        if lock.holder_id is None and draining:
            return DRAIN_REFUSED
        now = timezone.now()
        if lock.holder_id is None:
            lock.acquired_at = now
        lock.holder = user
        lock.last_activity = now
        lock.pending_release_at = None
        lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])
        return True
    return False


@transaction.atomic
def release(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id == user.id:
        _clear(lock)
        return True
    return False


def request_control(station, user, scope="station"):
    """The lock when another user holds it (normal request flow, also while draining:
    the holder decides, and a transfer is still drain-gated). ``DRAIN_REFUSED`` when
    the lock is free and the station drains, else None (free / requester holds it)."""
    return _request_control(station, user, scope, is_draining(station))


@transaction.atomic
def _request_control(station, user, scope, draining):
    lock = _locked(station, scope)
    if lock.holder_id is not None and lock.holder_id != user.id:
        return lock
    if lock.holder_id is None and draining:
        return DRAIN_REFUSED
    return None


def transfer(station, from_user, to_user_id, scope="station"):
    """True on transfer, ``DRAIN_REFUSED`` when only the drain blocks it, else False."""
    return _transfer(station, from_user, to_user_id, scope, is_draining(station))


@transaction.atomic
def _transfer(station, from_user, to_user_id, scope, draining):
    lock = _locked(station, scope)
    if lock.holder_id != from_user.id:
        return False
    # to_user_id is untrusted browser input: a bad/unknown id would raise an
    # IntegrityError on save, and a target who can't use the station would
    # become a "ghost holder" blocking everyone until T_idle/grace clears it.
    # Resolve + authorize the target before mutating; reject otherwise.
    from apps.accounts.models import User

    try:
        to_user = User.objects.get(pk=to_user_id)
    except (User.DoesNotExist, ValueError, TypeError):
        return False
    if not can(to_user, "control", "use"):
        return False
    if draining:
        return DRAIN_REFUSED
    now = timezone.now()
    lock.holder = to_user
    lock.acquired_at = now
    lock.last_activity = now
    lock.pending_release_at = None
    lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])
    return True


def preempt(station, user, scope="station"):
    """Override: take the lock from its current holder (allowed while draining — the
    drain stops NEW grants, it does not protect a holder). A FREE lock while draining
    is a new grant in disguise -> ``DRAIN_REFUSED``."""
    return _preempt(station, user, scope, is_draining(station))


@transaction.atomic
def _preempt(station, user, scope, draining):
    lock = _locked(station, scope)
    if lock.holder_id is None and draining:
        return DRAIN_REFUSED
    now = timezone.now()
    lock.holder = user
    lock.acquired_at = now
    lock.last_activity = now
    lock.pending_release_at = None
    lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])
    return True


@transaction.atomic
def touch(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id == user.id:
        lock.last_activity = timezone.now()
        lock.save(update_fields=["last_activity"])
        return True
    return False


@transaction.atomic
def holder_disconnected(station, user, grace_seconds, scope="station"):
    # KNOWN LIMITATION (D4): the lock is USER-owned and meant to be shared
    # across a user's tabs, but this arms the reconnect-grace on ANY holder
    # tab closing — even if another tab of the same user is still connected.
    # If that co-tab stays idle (no command/ptt/acquire) through the grace
    # window, the sweep will auto-free the lock. A correct fix needs per-tab
    # presence tracking (a ControlSession-style count), deferred to the same
    # follow-up as the viewer-cap infrastructure. For the single-operator D4
    # MVP this edge case is acceptable; documented in PR #90.
    lock = _locked(station, scope)
    if lock.holder_id == user.id:
        lock.pending_release_at = timezone.now() + timedelta(seconds=grace_seconds)
        lock.save(update_fields=["pending_release_at"])


@transaction.atomic
def holder_reconnected(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id == user.id and lock.pending_release_at is not None:
        lock.pending_release_at = None
        lock.save(update_fields=["pending_release_at"])


@transaction.atomic
def sweep_lock(station, now, idle_seconds, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id is None:
        return False
    grace_lapsed = lock.pending_release_at is not None and now >= lock.pending_release_at
    idle_lapsed = (
        lock.last_activity is not None
        and (now - lock.last_activity).total_seconds() > idle_seconds
    )
    if grace_lapsed or idle_lapsed:
        _clear(lock)
        return True
    return False


@transaction.atomic
def force_free(station, scope="station"):
    """Atomically clear the lock regardless of who holds it.

    Returns True iff the lock was held (i.e. a holder was cleared).
    Used by AgentControlConsumer on agent disconnect so a ghost lock
    never outlives the agent connection.
    """
    lk = _locked(station, scope)
    if lk.holder_id is None:
        return False
    _clear(lk)
    return True


def _clear(lock):
    lock.holder = None
    lock.acquired_at = None
    lock.last_activity = None
    lock.pending_release_at = None
    lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])


DRAIN_UNSET = object()


def status_with_drain(station, scope="station", drain=DRAIN_UNSET):
    """Lock frame payload plus ``drain`` (``{"since": iso}`` | ``None``).

    Computed once per status build, so a broadcast evaluates drain once for all
    recipients. A caller that already evaluated the drain for this state (e.g. the
    agent inventory path) passes it in as ``drain`` to skip the re-evaluation."""
    lock_obj, _ = ControlLock.objects.select_related("holder").get_or_create(
        station=station, scope=scope
    )
    status = lock_status(lock_obj)
    status["drain"] = drain_payload(station) if drain is DRAIN_UNSET else drain
    return status


def lock_status(lock):
    if lock.holder_id is None:
        return {"state": "free", "holder_id": None, "holder_username": None, "since": None}
    return {
        "state": "held",
        "holder_id": lock.holder_id,
        "holder_username": lock.holder.username if lock.holder else None,
        "since": lock.acquired_at.isoformat() if lock.acquired_at else None,
    }
