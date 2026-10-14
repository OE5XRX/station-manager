"""Unified permissions on the control + audio WebSockets (spec 2026-10-14 §3.4, §5.2).

ControlConsumer / AudioConsumer connect on ``control.view`` (Applicants watch +
hear RX), every operating action re-checks ``control.use`` (or the capability's
verb) against a freshly loaded user, and preempt follows ``can_override_station``.

No pytest-asyncio: scenarios run via asyncio.run() inside transactional tests,
reusing the helpers of the existing control/audio consumer suites.
"""

import asyncio

import pytest
from asgiref.sync import sync_to_async

from apps.accounts.models import AccountAuditLog, User
from apps.audio import gate as audio_gate
from apps.control import lock as lockmod
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationAuditLog,
)
from tests.test_audio_consumer import _agent_comm as _audio_agent_comm
from tests.test_audio_consumer import _browser as _audio_browser
from tests.test_audio_consumer import _drain_bytes_until as _audio_drain_bytes_until
from tests.test_audio_consumer import _drain_until as _audio_drain_until
from tests.test_audio_consumer import _load_bin as _audio_load_bin
from tests.test_audio_consumer import _load_json as _audio_load_json
from tests.test_control_consumer_lock import _browser_comm, _drain_until
from tests.test_control_write_role_enforcement import _agent_comm, _cmd


def _user(name, level):
    return User.objects.create(username=name, membership_level=level)


async def _set_level(user, level):
    await sync_to_async(User.objects.filter(pk=user.pk).update)(membership_level=level)


async def _acquire(comm):
    await comm.send_json_to({"type": "lock_acquire"})
    for _ in range(8):
        msg = await comm.receive_json_from()
        if msg.get("type") == "lock" and msg.get("you_hold") is True:
            return msg
    raise AssertionError("lock never acquired")


# ---------------------------------------------------------------------------
# ControlConsumer
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_applicant_connects_read_only_and_cannot_acquire():
    station = Station.objects.create(name="pc1", status="online")
    applicant = _user("pc-app", User.MembershipLevel.APPLICANT)

    async def scenario():
        comm = _browser_comm(applicant, station.id)
        assert (await comm.connect())[0] is True
        inventory = await comm.receive_json_from()  # connect succeeds (control.view)
        assert inventory["type"] == "inventory"
        lock = await _drain_until(comm, "lock")
        assert lock["state"] == "free"
        await comm.send_json_to({"type": "lock_acquire"})
        frame = await _drain_until(comm, "error")
        assert frame["code"] == "forbidden"
        assert frame["message"]
        # Connection stays open and the lock stays free.
        assert await comm.receive_nothing(timeout=0.2)
        await comm.disconnect()

    asyncio.run(scenario())
    assert lockmod.get_or_create_lock(station).holder_id is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mtype", ["lock_request", "lock_transfer"])
def test_applicant_lock_request_and_transfer_forbidden(mtype):
    station = Station.objects.create(name=f"pc-{mtype}", status="online")
    holder = _user(f"pc-h-{mtype}", User.MembershipLevel.MEMBER)
    applicant = _user(f"pc-a-{mtype}", User.MembershipLevel.APPLICANT)
    lockmod.acquire(station, holder)

    async def scenario():
        comm = _browser_comm(applicant, station.id)
        assert (await comm.connect())[0] is True
        await _drain_until(comm, "lock")
        await comm.send_json_to({"type": mtype, "to_user_id": holder.id, "request_id": "q1"})
        frame = await _drain_until(comm, "error")
        assert frame["code"] == "forbidden" and frame["request_id"] == "q1"
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_inactive_user_connect_denied_with_4403():
    station = Station.objects.create(name="pc-inact", status="online")
    user = _user("pc-inact", User.MembershipLevel.MEMBER)
    User.objects.filter(pk=user.pk).update(is_active=False)

    async def scenario():
        comm = _browser_comm(user, station.id)
        assert (await comm.connect())[0] is True
        frame = await comm.receive_json_from()
        assert frame["type"] == "error" and frame["code"] == "forbidden"
        assert (await comm.receive_output())["code"] == 4403

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_demoted_user_loses_ptt_without_reconnect(control_agent_auth):
    station = Station.objects.create(name="pc2", status="online")
    member = _user("pc-mem2", User.MembershipLevel.MEMBER)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        comm = _browser_comm(member, station.id)
        assert (await comm.connect())[0] is True
        await _acquire(comm)
        await _set_level(member, "applicant")
        await comm.send_json_to({"type": "ptt_keepalive", "request_id": "k1"})
        frame = await _drain_until(comm, "error")
        assert frame["code"] == "forbidden" and frame["request_id"] == "k1"
        # The keepalive never reached the agent.
        assert await agent.receive_nothing(timeout=0.3)
        await comm.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_member_cannot_set_filter_staff_can(control_agent_auth):
    station = Station.objects.create(name="pc3", status="online")
    member = _user("pc-mem3", User.MembershipLevel.MEMBER)
    staff = _user("pc-staff3", User.MembershipLevel.STAFF)

    async def run_as(user):
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        comm = _browser_comm(user, station.id)
        assert (await comm.connect())[0] is True
        await _acquire(comm)
        await comm.send_json_to(_cmd("filter_hpf", True, rid="f1"))
        if await agent.receive_nothing(timeout=0.5):
            outcome = ("error", await _drain_until(comm, "error"))
        else:
            outcome = ("relayed", await agent.receive_json_from(timeout=2))
        await comm.send_json_to({"type": "lock_release"})
        await _drain_until(comm, "lock")
        await comm.disconnect()
        await agent.disconnect()
        return outcome

    kind, frame = asyncio.run(run_as(member))
    assert kind == "error"
    assert frame["code"] == "forbidden" and frame["request_id"] == "f1"
    row = AccountAuditLog.objects.get(
        event_type=AccountAuditLog.EventType.PERMISSION_DENIED, actor=member
    )
    assert row.details["area"] == "control" and row.details["verb"] == "manage"
    assert row.details["channel"] == "ws"
    # The station audit trail keeps its own denied line.
    assert StationAuditLog.objects.filter(
        station=station, message__contains="denied (requires control.manage)"
    ).exists()

    kind, frame = asyncio.run(run_as(staff))
    assert kind == "relayed"
    assert frame["capability"] == "filter_hpf" and frame["value"] is True


@pytest.mark.django_db(transaction=True)
def test_region_manager_member_can_preempt_plain_member_cannot():
    region = Region.objects.create(name="PR", slug="pr")
    station = Station.objects.create(name="pc4", status="online", region=region)
    holder = _user("pc-h4", User.MembershipLevel.MEMBER)
    manager = _user("pc-rm4", User.MembershipLevel.MEMBER)
    RegionAssignment.objects.create(user=manager, region=region, role="manager")
    plain = _user("pc-plain4", User.MembershipLevel.MEMBER)
    lockmod.acquire(station, holder)

    async def scenario():
        pc = _browser_comm(plain, station.id)
        assert (await pc.connect())[0] is True
        await _drain_until(pc, "lock")
        await pc.send_json_to({"type": "lock_preempt"})
        frame = await _drain_until(pc, "error")
        assert frame["code"] == "forbidden"
        assert "Übernehmen der Steuerung" in frame["message"]
        assert "kalibrieren" not in frame["message"]
        await pc.disconnect()

        mc = _browser_comm(manager, station.id)
        assert (await mc.connect())[0] is True
        await _drain_until(mc, "lock")
        await mc.send_json_to({"type": "lock_preempt"})
        evt = await _drain_until(mc, "lock")
        assert evt["holder_id"] == manager.id and evt["you_hold"] is True
        await mc.disconnect()

    asyncio.run(scenario())
    # Override is not an area right: the plain member's denial is not a PERMISSION_DENIED row.
    assert not AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.PERMISSION_DENIED, actor=plain
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_applicant_station_admin_cannot_preempt():
    """control.use floor for assignment holders (user-approved tightening 2026-10-14)."""
    station = Station.objects.create(name="pc5", status="online")
    holder = _user("pc-h5", User.MembershipLevel.MEMBER)
    # Assignments can't be created for Applicants; the real case is a legacy assignment
    # that survived a demotion (queryset update bypasses the model validation).
    applicant = _user("pc-app5", User.MembershipLevel.MEMBER)
    StationAssignment.objects.create(user=applicant, station=station, role="admin")
    User.objects.filter(pk=applicant.pk).update(membership_level=User.MembershipLevel.APPLICANT)
    applicant.refresh_from_db()
    lockmod.acquire(station, holder)

    async def scenario():
        comm = _browser_comm(applicant, station.id)
        assert (await comm.connect())[0] is True
        await _drain_until(comm, "lock")
        await comm.send_json_to({"type": "lock_preempt"})
        frame = await _drain_until(comm, "error")
        assert frame["code"] == "forbidden"
        await comm.disconnect()

    asyncio.run(scenario())
    assert lockmod.get_or_create_lock(station).holder_id == holder.id


@pytest.mark.django_db(transaction=True)
def test_demoted_holder_get_command_forbidden_and_lock_revoked(control_agent_auth):
    """op:get touches the lock too: a demoted holder must not keep it alive with reads.
    The lock is released and PTT/TX route are cleared (TX path closed)."""

    station = Station.objects.create(name="pc7", status="online")
    member = _user("pc-mem7", User.MembershipLevel.MEMBER)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        comm = _browser_comm(member, station.id)
        assert (await comm.connect())[0] is True
        await _acquire(comm)
        await sync_to_async(audio_gate.set_ptt)(station, slot=0, module="fm0")
        await sync_to_async(audio_gate.set_tx_route)(station, slot=0, module="fm0")
        await _set_level(member, "applicant")
        await comm.send_json_to(_cmd("frequency", None, op="get", rid="g1"))
        frame = await _drain_until(comm, "error")
        assert frame["code"] == "forbidden" and frame["request_id"] == "g1"
        lost = await _drain_until(comm, "lock")
        assert lost["state"] == "free" and lost["you_hold"] is False
        assert await agent.receive_nothing(timeout=0.3)  # the get was not relayed
        await comm.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    lk = lockmod.get_or_create_lock(station)
    assert lk.holder_id is None and lk.last_activity is None
    state = audio_gate.get_wire_state(station)
    assert state["ptt_active"] is False
    assert state["tx_slot"] is None and state["tx_module"] == ""
    assert StationAuditLog.objects.filter(
        station=station, message__contains="lost control (requires control.use)"
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_silent_demoted_holder_loses_lock_on_periodic_recheck(monkeypatch):
    """A demoted holder who sends no frames must not keep the lock (and TX path)
    until the idle sweep: the periodic re-check revokes it."""
    from apps.accounts import permissions

    monkeypatch.setattr(permissions, "PERIODIC_RECHECK_SECONDS", 0.05)
    station = Station.objects.create(name="pc10", status="online")
    member = _user("pc-mem10", User.MembershipLevel.MEMBER)

    async def scenario():
        comm = _browser_comm(member, station.id)
        assert (await comm.connect())[0] is True
        await _acquire(comm)
        await sync_to_async(audio_gate.set_ptt)(station, slot=0, module="fm0")
        await sync_to_async(audio_gate.set_tx_route)(station, slot=0, module="fm0")
        await _set_level(member, "applicant")
        # No frame sent: only the periodic re-check can notice the demotion.
        lost = await _drain_until(comm, "lock")
        assert lost["state"] == "free" and lost["you_hold"] is False
        # Still a viewer (control.view): the feed stays open.
        assert await comm.receive_nothing(timeout=0.2)
        await comm.disconnect()

    asyncio.run(scenario())
    assert lockmod.get_or_create_lock(station).holder_id is None
    state = audio_gate.get_wire_state(station)
    assert state["ptt_active"] is False and state["tx_slot"] is None
    assert StationAuditLog.objects.filter(
        station=station, message__contains="lost control (requires control.use)"
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_demoted_holder_reconnecting_within_grace_does_not_get_lock_back():
    station = Station.objects.create(name="pc8", status="online")
    member = _user("pc-mem8", User.MembershipLevel.MEMBER)
    lockmod.acquire(station, member)
    lockmod.holder_disconnected(station, member, grace_seconds=60)
    User.objects.filter(pk=member.pk).update(membership_level=User.MembershipLevel.APPLICANT)

    async def scenario():
        comm = _browser_comm(member, station.id)
        assert (await comm.connect())[0] is True
        lock = await _drain_until(comm, "lock")
        assert lock["state"] == "free" and lock["you_hold"] is False
        await comm.disconnect()

    asyncio.run(scenario())
    assert lockmod.get_or_create_lock(station).holder_id is None


@pytest.mark.django_db(transaction=True)
def test_member_reconnecting_within_grace_keeps_lock():
    station = Station.objects.create(name="pc9", status="online")
    member = _user("pc-mem9", User.MembershipLevel.MEMBER)
    lockmod.acquire(station, member)
    lockmod.holder_disconnected(station, member, grace_seconds=60)

    async def scenario():
        comm = _browser_comm(member, station.id)
        assert (await comm.connect())[0] is True
        lock = await _drain_until(comm, "lock")
        assert lock["you_hold"] is True
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db
def test_transfer_target_needs_control_use():
    station = Station.objects.create(name="pc6", status="online")
    holder = _user("pc-h6", User.MembershipLevel.MEMBER)
    applicant = _user("pc-app6", User.MembershipLevel.APPLICANT)
    member = _user("pc-m6", User.MembershipLevel.MEMBER)
    lockmod.acquire(station, holder)
    assert lockmod.transfer(station, holder, applicant.pk) is False
    assert lockmod.transfer(station, holder, member.pk) is True


# ---------------------------------------------------------------------------
# AudioConsumer
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_applicant_hears_rx_but_mic_open_denied(audio_agent_auth):
    station = Station.objects.create(name="pa1", status="online")
    applicant = _user("pa-app1", User.MembershipLevel.APPLICANT)
    media_frame = _audio_load_bin("media_frame_slot0rx.bin")

    async def scenario():
        agent = _audio_agent_comm(station.id)
        assert (await agent.connect())[0] is True
        comm = _audio_browser(applicant, station.id)  # accepted
        assert (await comm.connect())[0] is True
        await asyncio.sleep(0.1)  # let the browser join the broadcast group
        await agent.send_json_to(_audio_load_json("advertise.json"))
        await _audio_drain_until(comm, "streams")
        # RX: the applicant's subscribe creates real demand at the agent ...
        await comm.send_json_to({"v": 1, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        sub = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert sub["type"] == "source_subscribe" and sub["stream_id"] == "slot0.rx"
        await asyncio.sleep(0.1)  # subscribe handler finishes its group_add
        # ... and RX media is delivered to them byte-identically.
        await agent.send_to(bytes_data=media_frame)
        assert await _audio_drain_bytes_until(comm) == media_frame
        await comm.send_json_to({"type": "mic_open"})
        frame = await _audio_drain_until(comm, "error")
        assert frame["code"] == "forbidden"
        await comm.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_demoted_user_mic_open_denied():
    station = Station.objects.create(name="pa2", status="online")
    member = _user("pa-mem2", User.MembershipLevel.MEMBER)

    async def scenario():
        comm = _audio_browser(member, station.id)
        assert (await comm.connect())[0] is True
        # Still a member: mic_open without the lock is the usual not_locked.
        await comm.send_json_to({"type": "mic_open"})
        frame = await _audio_drain_until(comm, "error")
        assert frame["code"] == "not_locked"
        await _set_level(member, "applicant")
        await comm.send_json_to({"type": "mic_open"})
        frame = await _audio_drain_until(comm, "error")
        assert frame["code"] == "forbidden"
        await comm.disconnect()

    asyncio.run(scenario())
