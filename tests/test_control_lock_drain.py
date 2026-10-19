# tests/test_control_lock_drain.py
"""ControlLock refuses new control grants while a module firmware update drains
the station (Teilbereich D, Task S4). Drain itself is patched; its predicate is
covered in tests/module_firmware/test_drain.py."""

from unittest import mock

import pytest

from apps.accounts.models import User
from apps.stations.models import Station

DRAINING = "apps.module_firmware.drain.is_draining"


def _user(name):
    return User.objects.create(username=name, membership_level=User.MembershipLevel.MEMBER)


@pytest.mark.django_db
def test_acquire_refused_while_draining():
    from apps.control import lock

    station = Station.objects.create(name="dr1")
    a, b = _user("dra1"), _user("drb1")
    with mock.patch(DRAINING, return_value=True):
        assert lock.acquire(station, a) is lock.DRAIN_REFUSED  # free lock, but draining
        assert lock.get_or_create_lock(station).holder_id is None
    assert lock.acquire(station, a) is True
    with mock.patch(DRAINING, return_value=True):
        assert lock.acquire(station, b) is False  # held by another user
    assert lock.get_or_create_lock(station).holder_id == a.id


@pytest.mark.django_db
def test_holder_reacquire_allowed_while_draining():
    from apps.control import lock

    station = Station.objects.create(name="dr2")
    a = _user("dra2")
    assert lock.acquire(station, a) is True
    with mock.patch(DRAINING, return_value=True):
        assert lock.acquire(station, a) is True
    assert lock.get_or_create_lock(station).holder_id == a.id


@pytest.mark.django_db
def test_request_to_holder_still_flows_while_draining():
    """Requesting from another holder is the normal flow (the holder decides; the
    transfer itself is drain-gated) — not a drain refusal."""
    from apps.control import lock

    station = Station.objects.create(name="dr3")
    a, b = _user("dra3"), _user("drb3")
    lock.acquire(station, a)
    assert lock.request_control(station, b).holder_id == a.id
    with mock.patch(DRAINING, return_value=True):
        assert lock.request_control(station, b).holder_id == a.id
        assert lock.request_control(station, a) is None  # the holder itself: no-op
    assert lock.get_or_create_lock(station).holder_id == a.id


@pytest.mark.django_db
def test_request_on_free_lock_while_draining_is_drain_refusal():
    from apps.control import lock

    station = Station.objects.create(name="dr3b")
    b = _user("drb3b")
    assert lock.request_control(station, b) is None
    with mock.patch(DRAINING, return_value=True):
        assert lock.request_control(station, b) is lock.DRAIN_REFUSED


@pytest.mark.django_db
def test_transfer_refused_while_draining():
    from apps.control import lock

    station = Station.objects.create(name="dr4")
    a, b = _user("dra4"), _user("drb4")
    lock.acquire(station, a)
    with mock.patch(DRAINING, return_value=True):
        assert lock.transfer(station, a, b.id) is lock.DRAIN_REFUSED
        assert lock.transfer(station, b, a.id) is False  # not the holder: no drain reason
    assert lock.get_or_create_lock(station).holder_id == a.id
    assert lock.transfer(station, a, b.id) is True


@pytest.mark.django_db
def test_release_and_preempt_unaffected():
    from apps.control import lock

    station = Station.objects.create(name="dr5")
    a, admin = _user("dra5"), _user("dradmin5")
    lock.acquire(station, a)
    with mock.patch(DRAINING, return_value=True):
        assert lock.preempt(station, admin) is True
        assert lock.get_or_create_lock(station).holder_id == admin.id
        assert lock.touch(station, admin) is True
        assert lock.release(station, admin) is True
        assert lock.get_or_create_lock(station).holder_id is None


@pytest.mark.django_db
def test_preempt_on_free_lock_refused_while_draining():
    """A preempt of a FREE lock is a new grant in disguise: refused during drain."""
    from apps.control import lock

    station = Station.objects.create(name="dr5b")
    admin = _user("dradmin5b")
    with mock.patch(DRAINING, return_value=True):
        assert lock.preempt(station, admin) is lock.DRAIN_REFUSED
        assert lock.get_or_create_lock(station).holder_id is None
    assert lock.preempt(station, admin) is True


def test_drain_refused_is_falsy():
    from apps.control import lock

    assert not lock.DRAIN_REFUSED


@pytest.mark.django_db
def test_drain_evaluated_before_row_lock():
    """The (multi-query) drain check runs before the ControlLock row is locked."""
    from apps.control import lock

    station = Station.objects.create(name="dr7")
    a = _user("dra7")
    order = []
    real_locked = lock._locked

    def locked(*args, **kwargs):
        order.append("lock")
        return real_locked(*args, **kwargs)

    with (
        mock.patch(DRAINING, side_effect=lambda st: order.append("drain") or False),
        mock.patch.object(lock, "_locked", side_effect=locked),
    ):
        assert lock.acquire(station, a) is True
        assert lock.request_control(station, a) is None
        assert lock.transfer(station, a, a.id) is True
        assert lock.preempt(station, a) is True
    assert order == ["drain", "lock"] * 4


@pytest.mark.django_db
def test_drain_check_uses_fresh_station_row():
    """Consumers hold a connection-cached Station; drain must see the DB row."""
    from apps.control import lock

    station = Station.objects.create(name="dr6")
    a = _user("dra6")
    seen = []
    with mock.patch(DRAINING, side_effect=lambda st: seen.append(st) or False):
        assert lock.acquire(station, a) is True
    assert len(seen) == 1
    assert seen[0].pk == station.pk
    assert seen[0] is not station


def test_firmware_drain_message():
    from apps.control import lock

    assert lock.FIRMWARE_DRAIN_MESSAGE == (
        "Firmware-Update ausstehend — neue Steuerung ist gesperrt, bis das Update "
        "abgeschlossen ist."
    )
