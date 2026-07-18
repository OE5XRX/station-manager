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
    now = timezone.now()
    lock.holder_id = to_user_id
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
