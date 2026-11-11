"""Every lock path runs one shared hook pair (``apps.control.lock_hooks``).

Part 1 pins today's frame sequence per path (regression net, written before the
refactor). Part 2 covers the registered hooks. No pytest-asyncio: scenarios run via
``asyncio.run``."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.accounts.models import User
from apps.audio.models import AudioGate
from apps.control import constants, lock
from apps.control.models import ActiveScan, ControlLock
from apps.stations.models import Station
from config.asgi import application

TO_AGENT_SCAN_STOP = "to_agent:scan_control"


def _agent_comm(station_id):
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _until(comm, pred, tries=20):
    for _ in range(tries):
        msg = await comm.receive_json_from(timeout=2)
        if pred(msg):
            return msg
    raise AssertionError("expected frame never arrived")


def _is_lock(held=None):
    def pred(m):
        return m.get("type") == "lock" and (held is None or (m.get("state") == "held") == held)

    return pred


async def _spy_on(station):
    """Join a fresh spy channel to the lock-relevant groups; returns its name."""
    from apps.audio import constants as audio_constants

    spy = f"lock-hooks-spy-{uuid.uuid4().hex}"
    layer = get_channel_layer()
    for group in (
        f"control_{station.id}",
        f"control_{station.id}_agent",
        audio_constants.browser_group(station.id),
        audio_constants.agent_group(station.id),
    ):
        await layer.group_add(group, spy)
    return spy


async def _seq(spy, timeout=0.4):
    """Labelled channel-layer traffic the spy saw, in send order."""
    layer = get_channel_layer()
    out = []
    while True:
        try:
            msg = await asyncio.wait_for(layer.receive(spy), timeout=timeout)
        except TimeoutError:
            return out
        if msg["type"] == "control.to_agent":
            out.append(f"to_agent:{msg['frame']['type']}")
        else:
            out.append(msg["type"])


def _station(name):
    return Station.objects.create(name=name, status="online")


def _user(name, level=User.MembershipLevel.MEMBER):
    return User.objects.create(username=name, membership_level=level)


def _scan_row(station, user):
    # "held" scans idle like a radio, so they never keep the lock alive in the sweep test.
    ActiveScan.objects.create(
        station=station, user=user, scan_id="s1", listen_s=2, hang_s=5, state="held"
    )


@database_sync_to_async
def _gate_dump(station):
    g = AudioGate.objects.get(station=station)
    return g.ptt_active, g.tx_slot, g.tx_module


@database_sync_to_async
def _arm_gate(station):
    g, _ = AudioGate.objects.get_or_create(station=station)
    g.ptt_active, g.ptt_slot, g.ptt_module = True, 0, "fm0"
    g.tx_slot, g.tx_module = 0, "fm0"
    g.save()


@database_sync_to_async
def _scan_count(station):
    return ActiveScan.objects.filter(station=station).count()


async def _connect_both(station, user):
    agent = _agent_comm(station.id)
    assert (await agent.connect())[0] is True
    browser = _browser(user, station.id)
    assert (await browser.connect())[0] is True
    await _until(browser, _is_lock())
    return agent, browser


async def _acquire(browser):
    await browser.send_json_to({"type": "lock_acquire"})
    await _until(browser, _is_lock(held=True))


# -- Part 1: regression net (passes on the pre-refactor code) -----------------------

FREE_SEQ = [TO_AGENT_SCAN_STOP, "audio.gate", "audio.gate", "control.lock"]


@pytest.mark.django_db(transaction=True)
def test_release_path_sequence(control_agent_auth):
    station, user = _station("lh-rel"), _user("lh-rel-u")

    async def scenario():
        agent, browser = await _connect_both(station, user)
        await _acquire(browser)
        await database_sync_to_async(_scan_row)(station, user)
        await _arm_gate(station)
        spy = await _spy_on(station)
        await browser.send_json_to({"type": "lock_release"})
        seq = await _seq(spy)
        gate = await _gate_dump(station)
        scans = await _scan_count(station)
        await browser.disconnect()
        await agent.disconnect()
        return seq, gate, scans

    seq, gate, scans = asyncio.run(scenario())
    assert seq == FREE_SEQ
    assert gate == (False, 0, "fm0")  # ptt cleared, tx route untouched
    assert scans == 0


@pytest.mark.django_db(transaction=True)
def test_revoke_path_clears_tx_route(control_agent_auth):
    station, user = _station("lh-rev"), _user("lh-rev-u")

    async def scenario():
        agent, browser = await _connect_both(station, user)
        await _acquire(browser)
        await database_sync_to_async(_scan_row)(station, user)
        await _arm_gate(station)
        await database_sync_to_async(User.objects.filter(pk=user.pk).update)(
            membership_level=User.MembershipLevel.APPLICANT
        )
        spy = await _spy_on(station)
        await browser.send_json_to(
            {
                "type": "command",
                "request_id": "r1",
                "slot": "slot0",
                "module": "fm0",
                "capability": "frequency",
                "op": "set",
                "value": 145.5,
            }
        )
        seq = await _seq(spy)
        gate = await _gate_dump(station)
        await browser.disconnect()
        await agent.disconnect()
        return seq, gate

    seq, gate = asyncio.run(scenario())
    assert seq == FREE_SEQ, seq
    assert gate == (False, None, "")  # ptt AND tx route cleared
    assert ControlLock.objects.get(station=station).holder_id is None


@pytest.mark.django_db(transaction=True)
def test_sweep_expiry_sequence(control_agent_auth, monkeypatch):
    monkeypatch.setattr(constants, "LOCK_SWEEP_INTERVAL_SECONDS", 0.05)
    station, user = _station("lh-exp"), _user("lh-exp-u")

    async def scenario():
        agent, browser = await _connect_both(station, user)
        await _acquire(browser)
        await database_sync_to_async(_scan_row)(station, user)
        await _arm_gate(station)
        spy = await _spy_on(station)
        await database_sync_to_async(ControlLock.objects.filter(station=station).update)(
            last_activity=timezone.now() - timedelta(hours=1)
        )
        seq = await _seq(spy)
        gate = await _gate_dump(station)
        await browser.disconnect()
        await agent.disconnect()
        return seq, gate

    seq, gate = asyncio.run(scenario())
    assert seq == FREE_SEQ
    assert gate == (False, 0, "fm0")


@pytest.mark.django_db(transaction=True)
def test_agent_disconnect_sequence(control_agent_auth):
    station, user = _station("lh-dis"), _user("lh-dis-u")

    async def scenario():
        agent, browser = await _connect_both(station, user)
        await _acquire(browser)
        await _arm_gate(station)
        spy = await _spy_on(station)
        await agent.disconnect()
        seq = await _seq(spy)
        gate = await _gate_dump(station)
        await browser.disconnect()
        return seq, gate

    seq, gate = asyncio.run(scenario())
    # No scan_stop to the agent (it is gone), PTT cleared, agent_offline before the lock.
    assert seq == ["audio.gate", "audio.gate", "control.agent_offline", "control.lock"]
    assert gate == (False, 0, "fm0")


@pytest.mark.django_db(transaction=True)
def test_acquire_sequence(control_agent_auth):
    station, user = _station("lh-acq"), _user("lh-acq-u")

    async def scenario():
        agent, browser = await _connect_both(station, user)
        spy = await _spy_on(station)
        await browser.send_json_to({"type": "lock_acquire"})
        seq = await _seq(spy)
        await browser.disconnect()
        await agent.disconnect()
        return seq

    assert asyncio.run(scenario()) == ["control.lock"]  # no gate frame, no scan stop


@pytest.mark.django_db(transaction=True)
def test_transfer_sequence(control_agent_auth):
    station, user = _station("lh-tr"), _user("lh-tr-u")
    other = _user("lh-tr-o")

    async def scenario():
        agent, browser = await _connect_both(station, user)
        await _acquire(browser)
        await database_sync_to_async(_scan_row)(station, user)
        await _arm_gate(station)
        spy = await _spy_on(station)
        await browser.send_json_to({"type": "lock_transfer", "to_user_id": other.id})
        seq = await _seq(spy)
        scans = await _scan_count(station)
        holder = await database_sync_to_async(
            lambda: ControlLock.objects.get(station=station).holder_id
        )()
        await browser.disconnect()
        await agent.disconnect()
        return seq, scans, holder

    seq, scans, holder = asyncio.run(scenario())
    assert seq == FREE_SEQ
    assert scans == 0
    assert holder == other.id


@pytest.mark.django_db(transaction=True)
def test_preempt_keeps_preemptors_scan(control_agent_auth):
    station, holder = _station("lh-pre"), _user("lh-pre-h")
    admin = _user("lh-pre-a", User.MembershipLevel.ADMIN)
    lock.acquire(station, holder)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(admin, station.id)
        assert (await browser.connect())[0] is True
        await _until(browser, _is_lock())
        await database_sync_to_async(_scan_row)(station, admin)  # the preemptor's own scan
        await _arm_gate(station)
        spy = await _spy_on(station)
        await browser.send_json_to({"type": "lock_preempt"})
        seq = await _seq(spy)
        gate = await _gate_dump(station)
        scans = await _scan_count(station)
        holder = await database_sync_to_async(
            lambda: ControlLock.objects.get(station=station).holder_id
        )()
        await browser.disconnect()
        await agent.disconnect()
        return seq, gate, scans, holder

    seq, gate, scans, holder = asyncio.run(scenario())
    assert seq == ["audio.gate", "audio.gate", "control.lock"]  # no scan_stop
    assert scans == 1
    assert gate[0] is False
    assert holder == admin.id


# -- Part 2: registered hooks ----------------------------------------------------------


@pytest.fixture
def hooks():
    from apps.control import lock_hooks

    calls = []

    async def on_release(station, reason):
        calls.append(("release", station.id, reason))

    async def on_acquire(station, holder, reason):
        calls.append(("acquire", station.id, holder.id, reason))

    lock_hooks.register_release_hook(on_release)
    lock_hooks.register_acquire_hook(on_acquire)
    yield calls
    lock_hooks.unregister_hooks(on_release, on_acquire)


@pytest.mark.django_db(transaction=True)
def test_registered_release_hook_called_with_reason_for_every_release_path(
    control_agent_auth, monkeypatch, hooks
):
    monkeypatch.setattr(constants, "LOCK_SWEEP_INTERVAL_SECONDS", 0.05)
    station, user = _station("lh-hr"), _user("lh-hr-u")

    async def scenario():
        agent, browser = await _connect_both(station, user)
        await _acquire(browser)
        await browser.send_json_to({"type": "lock_release"})  # released
        await _until(browser, _is_lock(held=False))
        await _acquire(browser)
        await database_sync_to_async(User.objects.filter(pk=user.pk).update)(
            membership_level=User.MembershipLevel.APPLICANT
        )
        await browser.send_json_to(
            {
                "type": "command",
                "request_id": "r1",
                "slot": "slot0",
                "module": "fm0",
                "capability": "frequency",
                "op": "set",
                "value": 145.5,
            }
        )  # revoked
        await _until(browser, _is_lock(held=False))
        await database_sync_to_async(User.objects.filter(pk=user.pk).update)(
            membership_level=User.MembershipLevel.MEMBER
        )
        await _acquire(browser)
        await database_sync_to_async(ControlLock.objects.filter(station=station).update)(
            last_activity=timezone.now() - timedelta(hours=1)
        )
        await _until(browser, _is_lock(held=False))  # expired
        await _acquire(browser)
        await agent.disconnect()  # agent_disconnect
        await asyncio.sleep(0.3)
        await browser.disconnect()

    asyncio.run(scenario())
    releases = [c[2] for c in hooks if c[0] == "release"]
    assert releases == ["released", "revoked", "expired", "agent_disconnect"]


@pytest.mark.django_db(transaction=True)
def test_registered_acquire_hook_called_for_acquire_transfer_preempt(control_agent_auth, hooks):
    station, user = _station("lh-ha"), _user("lh-ha-u")
    other = _user("lh-ha-o")
    admin = _user("lh-ha-a", User.MembershipLevel.ADMIN)

    async def scenario():
        agent, browser = await _connect_both(station, user)
        await _acquire(browser)
        await browser.send_json_to({"type": "lock_transfer", "to_user_id": other.id})
        await _until(browser, _is_lock(held=True))
        adm = _browser(admin, station.id)
        assert (await adm.connect())[0] is True
        await _until(adm, _is_lock())
        await adm.send_json_to({"type": "lock_preempt"})
        await _until(adm, _is_lock(held=True))
        await adm.disconnect()
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    acquires = [(c[2], c[3]) for c in hooks if c[0] == "acquire"]
    assert acquires == [
        (user.id, "acquired"),
        (other.id, "transferred"),
        (admin.id, "preempted"),
    ]


@pytest.mark.django_db(transaction=True)
def test_failing_hook_does_not_break_lock_path(control_agent_auth):
    from apps.control import lock_hooks

    async def boom(*args):
        raise RuntimeError("hook exploded")

    lock_hooks.register_release_hook(boom)
    lock_hooks.register_acquire_hook(boom)
    station, user = _station("lh-boom"), _user("lh-boom-u")

    async def scenario():
        agent, browser = await _connect_both(station, user)
        try:
            await _acquire(browser)  # lock broadcast still sent
            await browser.send_json_to({"type": "lock_release"})
            await _until(browser, _is_lock(held=False))
        finally:
            lock_hooks.unregister_hooks(boom)
            await browser.disconnect()
            await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_slow_hook_is_bounded_and_unknown_reason_is_safe(monkeypatch):
    from apps.control import lock_hooks

    monkeypatch.setattr(lock_hooks, "HOOK_TIMEOUT_S", 0.05)

    async def slow(*args):
        await asyncio.sleep(5)

    lock_hooks.register_release_hook(slow)
    station = _station("lh-slow")

    async def scenario():
        try:
            # documented-but-scanless reasons must not KeyError either
            await asyncio.wait_for(lock_hooks.on_lock_released(station, "agent_disconnect"), 2)
        finally:
            lock_hooks.unregister_hooks(slow)

    asyncio.run(scenario())
