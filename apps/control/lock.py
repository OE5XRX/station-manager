# apps/control/lock.py
"""Pure, synchronous TX-lock operations over ControlLock rows.

Every mutation is atomic + row-locked so concurrent workers can't both
acquire. Callers (consumers) wrap these in database_sync_to_async and
broadcast lock_status() afterwards.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ControlLock


def get_or_create_lock(station, scope="station"):
    lock, _ = ControlLock.objects.get_or_create(station=station, scope=scope)
    return lock


def _locked(station, scope):
    return ControlLock.objects.select_for_update().get_or_create(station=station, scope=scope)[0]


@transaction.atomic
def acquire(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id in (None, user.id):
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


@transaction.atomic
def request_control(station, user, scope="station"):
    lock = _locked(station, scope)
    if lock.holder_id is not None and lock.holder_id != user.id:
        return lock
    return None


@transaction.atomic
def transfer(station, from_user, to_user_id, scope="station"):
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
    if not to_user.can_use_station(station):
        return False
    now = timezone.now()
    lock.holder = to_user
    lock.acquired_at = now
    lock.last_activity = now
    lock.pending_release_at = None
    lock.save(update_fields=["holder", "acquired_at", "last_activity", "pending_release_at"])
    return True


@transaction.atomic
def preempt(station, user, scope="station"):
    lock = _locked(station, scope)
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


def lock_status(lock):
    if lock.holder_id is None:
        return {"state": "free", "holder_id": None, "holder_username": None, "since": None}
    return {
        "state": "held",
        "holder_id": lock.holder_id,
        "holder_username": lock.holder.username if lock.holder else None,
        "since": lock.acquired_at.isoformat() if lock.acquired_at else None,
    }
