# tests/test_control_lock.py
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.stations.models import Station


def _user(name):
    return User.objects.create(username=name, membership_level=User.MembershipLevel.MEMBER)


@pytest.mark.django_db
def test_acquire_from_free_then_second_user_blocked():
    from apps.control import lock

    station = Station.objects.create(name="l1")
    a, b = _user("a"), _user("b")

    assert lock.acquire(station, a) is True
    assert lock.acquire(station, b) is False  # already held by a
    assert lock.acquire(station, a) is True  # idempotent for same holder


@pytest.mark.django_db
def test_release_only_by_holder():
    from apps.control import lock

    station = Station.objects.create(name="l2")
    a, b = _user("a2"), _user("b2")
    lock.acquire(station, a)
    assert lock.release(station, b) is False
    assert lock.release(station, a) is True
    assert lock.acquire(station, b) is True  # now free -> b can take it


@pytest.mark.django_db
def test_targeted_transfer():
    from apps.control import lock

    station = Station.objects.create(name="l3")
    a, b = _user("a3"), _user("b3")
    lock.acquire(station, a)
    # request just reports the current holder
    assert lock.request_control(station, b).holder_id == a.id
    assert lock.transfer(station, a, b.id) is True
    assert lock.acquire(station, a) is False  # b holds it now
    assert lock.release(station, b) is True


@pytest.mark.django_db
def test_transfer_rejected_when_not_holder():
    from apps.control import lock

    station = Station.objects.create(name="l4")
    a, b, c = _user("a4"), _user("b4"), _user("c4")
    lock.acquire(station, a)
    assert lock.transfer(station, b, c.id) is False  # b is not the holder


@pytest.mark.django_db
def test_preempt_forces_holder():
    from apps.control import lock

    station = Station.objects.create(name="l5")
    a, admin = _user("a5"), _user("admin5")
    lock.acquire(station, a)
    assert lock.preempt(station, admin) is True
    assert lock.acquire(station, a) is False  # admin holds it


@pytest.mark.django_db
def test_sweep_idle_frees_lock():
    from apps.control import lock

    station = Station.objects.create(name="l6")
    a = _user("a6")
    lock.acquire(station, a)
    lock.touch(station, a)
    now = timezone.now() + timedelta(seconds=600)  # 10 min later
    assert lock.sweep_lock(station, now=now, idle_seconds=120) is True
    assert lock.acquire(station, _user("z6")) is True  # was freed


@pytest.mark.django_db
def test_sweep_reconnect_grace():
    from apps.control import lock

    station = Station.objects.create(name="l7")
    a = _user("a7")
    lock.acquire(station, a)
    lock.holder_disconnected(station, a, grace_seconds=12)
    # Before the grace deadline: still held.
    soon = timezone.now() + timedelta(seconds=5)
    assert lock.sweep_lock(station, now=soon, idle_seconds=999999) is False
    # Reconnect clears the pending release.
    lock.holder_reconnected(station, a)
    later = timezone.now() + timedelta(seconds=30)
    assert lock.sweep_lock(station, now=later, idle_seconds=999999) is False
    # Disconnect again, let the grace lapse -> freed.
    lock.holder_disconnected(station, a, grace_seconds=12)
    past_grace = timezone.now() + timedelta(seconds=30)
    assert lock.sweep_lock(station, now=past_grace, idle_seconds=999999) is True


@pytest.mark.django_db
def test_force_free_returns_true_when_held():
    from apps.control import lock

    station = Station.objects.create(name="l8")
    a = _user("a8")
    lock.acquire(station, a)
    assert lock.force_free(station) is True
    lk = lock.get_or_create_lock(station)
    assert lk.holder_id is None


@pytest.mark.django_db
def test_force_free_returns_false_when_already_free():
    from apps.control import lock

    station = Station.objects.create(name="l9")
    assert lock.force_free(station) is False
