"""``PassiveSupervisor`` driven with a fake agent link (no websockets).

The fake answers every frame with an ok ``result`` unless ``hold`` is set (then the test
replies by hand) or the capability/frame type is listed in ``fail``."""

import asyncio
from datetime import timedelta

import pytest
from channels.db import database_sync_to_async
from django.utils import timezone

from apps.control.models import ControlLock, PersistedCapability
from apps.passive.models import (
    HandoverProfile,
    PassiveServiceConfig,
    PassiveServiceRun,
    StationPassiveConfig,
)
from apps.passive.supervisor import PassiveSupervisor
from apps.stations.models import Station, StationAuditLog
from tests.test_passive_profiles import make_module

pytestmark = pytest.mark.django_db(transaction=True)

State = PassiveServiceRun.State


class FakeAgent:
    def __init__(self):
        self.frames: list[dict] = []
        self.published: list[dict] = []
        self.hold = False
        self.fail: dict[str, str] = {}  # capability or frame type -> error code
        self.sup: PassiveSupervisor | None = None

    async def send(self, frame):
        self.frames.append(frame)
        if not self.hold:
            await self.reply(frame)

    async def stopped_before_ack(self, frame):
        """Like the real host: ``decoder_state stopped`` goes out before the stop result."""
        if frame["type"] == "decoder_stop":
            await self.sup.on_decoder_state(
                {"decoder": "aprs", "slot": frame["slot"], "state": "stopped"}
            )

    async def reply(self, frame):
        await self.stopped_before_ack(frame)
        key = frame.get("capability") or frame["type"]
        code = self.fail.get(key)
        result = {"type": "result", "request_id": frame["request_id"], "ok": code is None}
        if code:
            result["error"] = {"code": code, "msg": ""}
        await self.sup.on_result(result)

    async def broadcast(self, payload):
        self.published.append(payload)

    def sent(self, type_=None):
        return [f for f in self.frames if type_ is None or f["type"] == type_]

    def caps(self):
        return [(f["capability"], f["value"]) for f in self.sent("command")]

    async def wait_frames(self, n):
        for _ in range(200):
            if len(self.frames) >= n:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {len(self.frames)} frames, wanted {n}")


HANDOVER = [("frequency", 145.5), ("rx_tone", "none"), ("tx_tone", "none"), ("squelch", 1)]


class Clock:
    def __init__(self):
        self.t = timezone.now()

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def env(station):
    PassiveServiceConfig.objects.create(
        service="aprs",
        enabled=True,
        idle_minutes=10,
        profile={
            "frequency": 144.8,
            "bandwidth": "25",
            "rx_tone": "none",
            "tx_tone": "none",
            "squelch": 0,
        },
    )
    HandoverProfile.get()
    StationPassiveConfig.objects.create(
        station=station, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    make_module(station)
    agent = FakeAgent()
    clock = Clock()
    sup = PassiveSupervisor(
        station.pk, agent.send, agent.broadcast, now=clock, command_timeout=0.5
    )
    agent.sup = sup
    sup.set_agent_features(["decoders"])
    sup._reconciled = True  # the snapshot gate has its own tests
    return station, sup, agent, clock


def run_of(station):
    return PassiveServiceRun.objects.filter(station=station).order_by("-pk").first()


async def started(station, sup, agent):
    """Tick a fresh supervisor into a running run."""
    await sup.tick()
    await sup.on_decoder_state({"decoder": "aprs", "slot": 1, "state": "running"})
    return await database_sync_to_async(run_of)(station)


def test_tick_ineligible_does_nothing(env):
    station, sup, agent, _ = env
    PassiveServiceConfig.objects.update(enabled=False)

    async def go():
        await sup.tick()

    asyncio.run(go())
    assert agent.frames == []
    assert run_of(station) is None
    assert agent.published == []  # an unconfigured service stays silent


def test_tick_starts_profile_then_decoder(env):
    station, sup, agent, _ = env

    async def go():
        await sup.tick()
        await sup.on_decoder_state({"decoder": "aprs", "slot": 1, "state": "running"})

    asyncio.run(go())
    assert agent.caps() == [
        ("frequency", 144.8),
        ("bandwidth", "25"),
        ("rx_tone", "none"),
        ("tx_tone", "none"),
        ("squelch", 0),
    ]
    assert all(f["request_id"].startswith("passive-") for f in agent.frames)
    start = agent.sent("decoder_start")[0]
    assert (start["decoder"], start["slot"], start["params"]) == ("aprs", 1, {})
    assert agent.frames[-1] is start
    run = run_of(station)
    assert (run.state, run.slot, run.module_id) == (State.RUNNING, "1", "fm-1")
    assert sup.protected_caps() == {("1", "fm-1", c) for c, _v in agent.caps()}
    audit = StationAuditLog.objects.get(event_type="passive_service")
    assert audit.user is None and audit.message.startswith("system:passive_services")
    assert agent.published[-1]["state"] == "running"


def test_system_writes_never_persisted(env):
    station, sup, agent, _ = env
    asyncio.run(sup.tick())
    assert PersistedCapability.objects.count() == 0


def test_profile_command_failure_backs_off(env):
    station, sup, agent, clock = env
    agent.fail["bandwidth"] = "validation_failed"

    async def go():
        await sup.tick()
        n = len(agent.frames)
        await sup.tick()  # still inside the 60 s back-off
        assert len(agent.frames) == n
        clock.advance(seconds=61)
        agent.fail.clear()
        await sup.tick()

    asyncio.run(go())
    runs = list(PassiveServiceRun.objects.order_by("pk"))
    assert [r.state for r in runs] == [State.ERROR, State.STARTING]
    assert runs[0].end_reason == "start_failed" and runs[0].last_error == "validation_failed"
    assert runs[0].ended_at is not None
    assert not any(f["type"] == "decoder_start" for f in agent.frames[:3])


def test_backoff_doubles_and_caps():
    from apps.passive.supervisor import _backoff_seconds

    assert [_backoff_seconds(n) for n in (1, 2, 3, 5, 6, 9)] == [60, 120, 240, 900, 900, 900]


def test_start_timeout_result_counts_as_failure(env):
    station, sup, agent, _ = env
    agent.hold = True

    async def go():
        await sup.tick()  # first command never answered -> timeout

    asyncio.run(go())
    assert run_of(station).state == State.ERROR
    assert run_of(station).last_error == "timeout"


def test_preempt_during_start_aborts_before_decoder_start(env):
    station, sup, agent, _ = env
    agent.hold = True

    async def go():
        ticking = asyncio.create_task(sup.tick())
        await agent.wait_frames(1)
        await agent.reply(agent.frames[0])  # first command done, second in flight
        await agent.wait_frames(2)
        agent.hold = False
        await sup.preempt("acquired", handover=True)
        await ticking

    asyncio.run(go())
    assert not agent.sent("decoder_start") and not agent.sent("decoder_stop")
    assert agent.caps()[-4:] == HANDOVER
    assert [c for c, _v in agent.caps()].count("frequency") == 2  # profile once, hand-over once
    run = run_of(station)
    assert (run.state, run.end_reason) == (State.STOPPED, "acquired")
    assert sup.protected_caps() == set()


def test_lock_taken_between_commands_and_start_aborts(env):
    station, sup, agent, _ = env
    holder = _user()
    original = agent.send

    async def send(frame):
        await original(frame)
        if frame.get("capability") == "squelch":  # last profile command
            await database_sync_to_async(ControlLock.objects.update_or_create)(
                station=station, scope="station", defaults={"holder": holder}
            )

    sup._send = send
    asyncio.run(sup.tick())
    assert not agent.sent("decoder_start")
    run = run_of(station)
    assert (run.state, run.end_reason) == (State.STOPPED, "acquired")
    assert agent.caps()[-4:] == HANDOVER


def _user():
    from apps.accounts.models import User

    return User.objects.create(username="holder", membership_level=User.MembershipLevel.MEMBER)


def test_preempt_running_sends_stop_then_handover_with_persisted_volume(env):
    station, sup, agent, _ = env
    PersistedCapability.objects.create(
        station=station, slot="1", module_id="fm-1", capability="volume", value=7
    )

    async def go():
        await started(station, sup, agent)
        agent.frames.clear()
        await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert [f["type"] for f in agent.frames] == ["decoder_stop"] + ["command"] * 5
    assert agent.caps() == [*HANDOVER, ("volume", 7)]
    run = run_of(station)
    assert (run.state, run.end_reason) == (State.STOPPED, "acquired")
    assert agent.published[-1]["state"] == "off"


def test_handover_failure_does_not_block_close(env):
    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        agent.fail["frequency"] = "timeout"
        await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert run_of(station).state == State.STOPPED


def test_request_preempt_does_not_block_on_agent(env):
    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        agent.hold = True
        agent.frames.clear()
        sup.request_preempt("acquired", handover=True)  # sync: returns at once
        await agent.wait_frames(1)
        assert agent.frames[0]["type"] == "decoder_stop"
        agent.hold = False
        await agent.reply(agent.frames[0])
        await sup.drain()

    asyncio.run(go())
    assert run_of(station).end_reason == "acquired"


def test_tick_preempts_when_lock_held_without_hook(env):
    station, sup, agent, _ = env
    holder = _user()

    async def go():
        await started(station, sup, agent)
        await database_sync_to_async(ControlLock.objects.create)(station=station, holder=holder)
        agent.frames.clear()
        await sup.tick()

    asyncio.run(go())
    assert agent.sent("decoder_stop")
    assert agent.caps() == HANDOVER
    assert run_of(station).end_reason == "acquired"


def test_updating_or_draining_stops_without_handover(env):
    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        await database_sync_to_async(Station.objects.filter(pk=station.pk).update)(
            status=Station.Status.UPDATING
        )
        agent.frames.clear()
        await sup.tick()

    asyncio.run(go())
    assert [f["type"] for f in agent.frames] == ["decoder_stop"]
    assert run_of(station).end_reason == "updating"


def test_disabled_while_running_stops(env):
    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        await database_sync_to_async(PassiveServiceConfig.objects.update)(enabled=False)
        await sup.tick()

    asyncio.run(go())
    assert run_of(station).end_reason == "disabled"


def test_decoder_error_closes_run_and_backs_off(env):
    station, sup, agent, clock = env

    async def go():
        await started(station, sup, agent)
        agent.frames.clear()
        await sup.on_decoder_state(
            {"decoder": "aprs", "slot": 1, "state": "error", "detail": "unit_failed: x"}
        )
        await sup.drain()
        n = len(agent.frames)
        await sup.tick()
        assert len(agent.frames) == n  # back-off

    asyncio.run(go())
    run = run_of(station)
    assert (run.state, run.end_reason) == (State.ERROR, "decoder_error")
    assert run.last_error == "unit_failed: x"
    assert [f["type"] for f in agent.frames] == ["decoder_stop"]


def test_snapshot_empty_closes_open_run(env):
    station, sup, agent, _ = env
    sup._reconciled = False
    PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="1", module_id="fm-1", state=State.AGENT_OFFLINE
    )

    async def go():
        await sup.on_snapshot({"decoders": []})

    asyncio.run(go())
    run = run_of(station)
    assert (run.state, run.end_reason) == (State.STOPPED, "agent_restarted")


def test_snapshot_running_without_run_sends_stop(env):
    station, sup, agent, _ = env
    sup._reconciled = False

    async def go():
        await sup.on_snapshot({"decoders": [{"decoder": "aprs", "slot": 1, "state": "running"}]})
        await sup.drain()

    asyncio.run(go())
    stop = agent.sent("decoder_stop")
    assert len(stop) == 1 and stop[0]["slot"] == 1
    assert run_of(station) is None


def test_disconnect_marks_agent_offline_and_reconnect_resumes(env):
    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        await sup.on_agent_disconnect()
        assert (await database_sync_to_async(run_of)(station)).state == State.AGENT_OFFLINE
        # a new connection = a new supervisor
        agent2 = FakeAgent()
        sup2 = PassiveSupervisor(station.pk, agent2.send, agent2.broadcast, command_timeout=0.5)
        agent2.sup = sup2
        sup2.set_agent_features(["decoders"])
        await sup2.on_snapshot({"decoders": [{"decoder": "aprs", "slot": 1, "state": "running"}]})
        await sup2.on_inventory()
        await sup2.drain()
        return agent2, sup2

    agent2, sup2 = asyncio.run(go())
    assert run_of(station).state == State.RUNNING
    assert agent2.frames == []
    assert ("1", "fm-1", "squelch") in sup2.protected_caps()


def test_disconnect_during_start_closes_run(env):
    station, sup, agent, _ = env
    agent.hold = True

    async def go():
        ticking = asyncio.create_task(sup.tick())
        await agent.wait_frames(1)
        await sup.on_agent_disconnect()
        await ticking

    asyncio.run(go())
    run = run_of(station)
    assert (run.state, run.end_reason) == (State.STOPPED, "agent_disconnect")
    assert not agent.sent("decoder_start")


def test_agent_without_decoders_feature_is_too_old(env):
    station, sup, agent, _ = env
    sup.set_agent_features(None)
    sup.set_agent_features([])

    async def go():
        await sup.tick()

    asyncio.run(go())
    assert agent.frames == []
    assert agent.published[-1]["reason"] == "agent_too_old"


def test_unsupported_start_result_sets_agent_too_old(env):
    station, sup, agent, clock = env
    agent.fail["decoder_start"] = "unsupported"

    async def go():
        await sup.tick()
        clock.advance(seconds=61)
        await sup.tick()

    asyncio.run(go())
    assert agent.published[-1]["reason"] == "agent_too_old"
    assert len(agent.sent("decoder_start")) == 1
    sup.set_agent_features(["decoders"])  # next inventory clears it
    assert sup._unsupported is False


def test_idle_wait_blocks_start_until_timer_elapsed(env):
    station, sup, agent, clock = env
    ControlLock.objects.create(station=station, released_at=clock() - timedelta(minutes=2))

    async def go():
        await sup.tick()
        assert agent.frames == []
        clock.advance(minutes=9)
        await sup.tick()

    asyncio.run(go())
    assert agent.sent("decoder_start")


def test_stale_starting_run_is_failed(env):
    station, sup, agent, clock = env
    run = PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="1", module_id="fm-1", state=State.STARTING
    )
    PassiveServiceRun.objects.filter(pk=run.pk).update(started_at=clock() - timedelta(minutes=5))

    async def go():
        await sup.tick()
        await sup.drain()

    asyncio.run(go())
    run.refresh_from_db()
    assert (run.state, run.last_error) == (State.ERROR, "start_timeout")


def test_no_start_before_the_snapshot_then_fallback_after_wait(env):
    station, sup, agent, clock = env
    sup._reconciled = False
    clock.advance(seconds=0)
    sup._features_at = clock()

    async def go():
        await sup.tick()
        assert agent.frames == []
        assert await database_sync_to_async(run_of)(station) is None
        clock.advance(seconds=31)  # snapshot never came: reconcile as empty, then start
        await sup.tick()
        await sup.tick()

    asyncio.run(go())
    assert agent.sent("decoder_start")


def test_snapshot_during_wait_unblocks_start(env):
    station, sup, agent, clock = env
    sup._reconciled = False
    sup._features_at = clock()

    async def go():
        await sup.on_snapshot({"decoders": []})
        await sup.tick()

    asyncio.run(go())
    assert agent.sent("decoder_start")


def test_prepare_seeds_protected_caps_from_open_run(env):
    station, sup, agent, _ = env
    PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="1", module_id="fm-1", state=State.AGENT_OFFLINE
    )
    asyncio.run(sup.prepare())
    assert ("1", "fm-1", "squelch") in sup.protected_caps()


def test_handover_skipped_while_keyed(env, monkeypatch):
    station, sup, agent, _ = env
    monkeypatch.setattr("apps.audio.gate.ptt_keyed", lambda s: True)

    async def go():
        await started(station, sup, agent)
        agent.frames.clear()
        await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert [f["type"] for f in agent.frames] == ["decoder_stop"]
    assert run_of(station).end_reason == "acquired"


def test_handover_leaves_caps_the_user_set(env):
    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        sup.note_user_set(("1", "fm-1", "frequency"))
        agent.frames.clear()
        await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert ("frequency", 145.5) not in agent.caps()
    assert ("squelch", 1) in agent.caps()


def test_disconnect_during_handover_does_not_wait_for_timeouts(env):
    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        agent.hold = True
        agent.frames.clear()
        preempting = asyncio.create_task(sup.preempt("acquired", handover=True))
        await agent.wait_frames(1)  # decoder_stop in flight
        begin = asyncio.get_running_loop().time()
        await sup.on_agent_disconnect()
        await preempting
        return asyncio.get_running_loop().time() - begin

    elapsed = asyncio.run(go())
    assert elapsed < 0.4  # command_timeout is 0.5 s per command
    assert len(agent.frames) == 1
    assert run_of(station).end_reason == "acquired"


def test_run_never_left_stopping_when_handover_blows_up(env, monkeypatch):
    station, sup, agent, _ = env

    def boom(run):
        raise RuntimeError("db gone")

    async def go():
        await started(station, sup, agent)
        monkeypatch.setattr(sup, "_handover_plan", boom)
        with pytest.raises(RuntimeError):
            await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert run_of(station).state == State.STOPPED


def test_decoder_error_detail_survives_stop_announcement(env):
    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        await sup.on_decoder_state(
            {"decoder": "aprs", "slot": 1, "state": "error", "detail": "unit_failed: x"}
        )
        await sup.on_decoder_state(  # a second error frame must not pile up a second task
            {"decoder": "aprs", "slot": 1, "state": "error", "detail": "unit_failed: x"}
        )
        await sup.drain()

    asyncio.run(go())
    run = run_of(station)
    assert (run.state, run.end_reason, run.last_error) == (
        State.ERROR,
        "decoder_error",
        "unit_failed: x",
    )
    assert len(agent.sent("decoder_stop")) == 1


def test_superseded_connection_does_nothing(env):
    station, sup, agent, _ = env

    async def stale():
        return False

    sup._is_current = stale

    async def go():
        await sup.tick()
        await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert agent.frames == [] and run_of(station) is None


def test_start_failure_applies_handover_profile(env):
    station, sup, agent, _ = env
    agent.fail["squelch"] = "validation_failed"
    asyncio.run(sup.tick())
    assert agent.caps()[-4:] == HANDOVER
    assert run_of(station).state == State.ERROR


def test_orphan_stops_are_capped_and_validated(env):
    station, sup, agent, _ = env
    sup._reconciled = False
    listed = [{"decoder": "aprs", "slot": n} for n in range(20)]
    listed.append({"decoder": "aprs", "slot": "\u00b2"})

    async def go():
        await sup.on_snapshot({"decoders": listed})
        await sup.drain()

    asyncio.run(go())
    assert len(agent.sent("decoder_stop")) == 8


def test_user_touches_do_not_leak_into_later_hand_overs(env):
    station, sup, agent, _ = env
    sup.note_user_set(("1", "fm-1", "frequency"))  # an earlier holder's set

    async def go():
        await started(station, sup, agent)  # run creation clears stale touches
        agent.frames.clear()
        await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert ("frequency", 145.5) in agent.caps()


def test_wildcard_touch_skips_the_whole_hand_over(env):
    from apps.passive.supervisor import WILDCARD_TOUCH

    station, sup, agent, _ = env

    async def go():
        await started(station, sup, agent)
        sup.note_user_set(WILDCARD_TOUCH)  # user started a scan
        agent.frames.clear()
        await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert [f["type"] for f in agent.frames] == ["decoder_stop"]


def test_handover_stops_when_ptt_keys_mid_way(env, monkeypatch):
    station, sup, agent, _ = env
    answers = iter([False, True, True, True, True, True])
    monkeypatch.setattr("apps.audio.gate.ptt_keyed", lambda s: next(answers))

    async def go():
        await started(station, sup, agent)
        agent.frames.clear()
        await sup.preempt("acquired", handover=True)

    asyncio.run(go())
    assert [f["type"] for f in agent.frames] == ["decoder_stop", "command"]
    assert run_of(station).state == State.STOPPED


def test_lock_grant_right_after_run_creation_stops_before_any_command(env, monkeypatch):
    station, sup, agent, _ = env
    calls = {"n": 0}

    def free(self=None):
        calls["n"] += 1
        return calls["n"] == 1  # free for run creation, taken right after

    monkeypatch.setattr(sup, "_lock_is_free", free)
    asyncio.run(sup.tick())
    assert agent.frames == []  # nothing tuned, no hand-over either
    run = run_of(station)
    assert (run.state, run.end_reason) == (State.STOPPED, "acquired")


def test_preempting_an_agent_offline_run_still_stops_the_decoder(env):
    station, sup, agent, _ = env
    PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="1", module_id="fm-1", state=State.AGENT_OFFLINE
    )
    asyncio.run(sup.preempt("acquired", handover=False))
    assert [f["type"] for f in agent.frames] == ["decoder_stop"]


def test_cancelled_decoder_died_never_leaves_stopping(env):
    station, sup, agent, _ = env

    async def go():
        run = await started(station, sup, agent)
        agent.hold = True
        dying = asyncio.create_task(sup._decoder_died(run, "boom"))
        await agent.wait_frames(len(agent.frames) + 1)
        dying.cancel()
        await asyncio.gather(dying, return_exceptions=True)

    asyncio.run(go())
    run = run_of(station)
    assert (run.state, run.end_reason) == (State.ERROR, "decoder_error")


def test_overlong_slot_in_snapshot_is_ignored(env):
    station, sup, agent, _ = env
    sup._reconciled = False

    async def go():
        await sup.on_snapshot({"decoders": [{"decoder": "aprs", "slot": "1" * 5000}]})
        await sup.drain()

    asyncio.run(go())
    assert agent.frames == []
