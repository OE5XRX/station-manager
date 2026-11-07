"""Server scan orchestration (repeater-scanner spec §7, plan Task 9b): ``scan_start`` /
``scan_control`` browser frames, agent ``scan_state``, ``ActiveScan`` lifecycle, lock hooks
(Review Focus 5), list edits -> ``scan_update``, idle-sweep skip. Async scenarios run via
asyncio.run() (no pytest-asyncio)."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.accounts.models import User
from apps.audio import gate as audio_gate
from apps.control import capability_policy, lock, scan
from apps.control.models import ActiveScan, ControlLock, PersistedCapability, StationModule
from apps.repeaters.models import Repeater, ScanList, ScanListEntry
from apps.stations.models import Station, StationAuditLog
from config.asgi import application

VHF = {"name": "vhf", "min": 134.0, "max": 174.0}


def _desc():
    rng = [VHF]
    return [
        {"name": "tx_frequency", "kind": "setting", "type": "float", "ranges": rng},
        {"name": "rx_frequency", "kind": "setting", "type": "float", "ranges": rng},
        {"name": "tx_tone", "kind": "setting", "type": "string"},
        {"name": "rx_tone", "kind": "setting", "type": "string"},
        {"name": "carrier", "kind": "telemetry", "type": "bool", "readonly": True},
    ]


def _setup(name, coords=True, level=User.MembershipLevel.MEMBER):
    station = Station.objects.create(
        name=name,
        status="online",
        latitude=Decimal("48.0") if coords else None,
        longitude=Decimal("14.0") if coords else None,
    )
    user = User.objects.create(username=f"u-{name}", membership_level=level)
    StationModule.objects.create(
        station=station,
        slot="1",
        module_id="fm0",
        type="fm_transceiver",
        online=True,
        capability_descriptor=_desc(),
    )
    return station, user


def _rep(callsign="OE5XLL", out="145.600", inp="145.000", near=True):
    return Repeater.objects.create(
        callsign=callsign,
        output_frequency=Decimal(out),
        input_frequency=Decimal(inp),
        input_tone="77.0",
        latitude=Decimal("48.1") if near else None,
        longitude=Decimal("14.0") if near else None,
        source="manual",
    )


def _list_with_freq(user, freq="145.500", name="calling"):
    scan_list = ScanList.objects.create(owner=user, name=name)
    ScanListEntry.objects.create(scan_list=scan_list, position=0, frequency=Decimal(freq))
    return scan_list


def _start(rid="s1", radius_km=50, list_ids=(), **kw):
    return {
        "type": "scan_start",
        "request_id": rid,
        "radius_km": radius_km,
        "list_ids": list(list_ids),
        "band_map": {},
        "listen_s": 2,
        "hang_s": 5,
        **kw,
    }


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


def _code(frame):
    return frame.get("code") or (frame.get("error") or {}).get("code")


async def _connect(station, user, acquire=True):
    agent = _agent_comm(station.id)
    assert (await agent.connect())[0] is True
    browser = _browser(user, station.id)
    assert (await browser.connect())[0] is True
    if acquire:
        await browser.send_json_to({"type": "lock_acquire"})
        await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")
    return agent, browser


async def _started(agent, browser, frame):
    await browser.send_json_to(frame)
    return await _until(agent, lambda m: m.get("type") == "scan_start")


def _run_start(station, user, frame, acquire=True):
    """("relayed"|"error", agent_frame|browser_frame, ActiveScan rows before teardown)."""

    async def scenario():
        agent, browser = await _connect(station, user, acquire)
        await browser.send_json_to(frame)
        if await agent.receive_nothing(timeout=0.5):
            outcome = ("error", await _until(browser, lambda m: m.get("type") == "error"))
        else:
            outcome = ("relayed", await agent.receive_json_from(timeout=2))
        # Snapshot before teardown: the agent disconnect below ends the scan.
        rows = await database_sync_to_async(lambda: list(ActiveScan.objects.all()))()
        outcome += (rows,)
        await browser.disconnect()
        await agent.disconnect()
        return outcome

    return asyncio.run(scenario())


# -- scan_start ----------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_scan_start_relays_entries(control_agent_auth):
    station, user = _setup("sc-ok")
    rep = _rep()
    calling = _list_with_freq(user)
    kind, frame, rows = _run_start(station, user, _start(list_ids=[calling.pk]))
    assert kind == "relayed"
    assert frame["type"] == "scan_start" and frame["v"] == 1
    assert frame["listen_s"] == 2.0 and frame["hang_s"] == 5.0
    assert [e["ref"] for e in frame["entries"]] == [
        f"f:{calling.entries.get().pk}",
        f"r:{rep.pk}",
    ]
    first, second = frame["entries"]
    assert first["slot"] == 1 and first["module"] == "fm0"  # slot as int to the agent
    assert (first["rx_frequency"], first["tx_frequency"]) == (145.5, 145.5)
    assert (second["rx_frequency"], second["tx_frequency"]) == (145.6, 145.0)
    assert second["tx_tone"] == "77.0"
    (row,) = rows
    assert row.scan_id == frame["scan_id"] and row.user == user
    assert row.list_ids == [calling.pk] and row.radius_km == 50.0
    assert not ActiveScan.objects.exists()  # agent gone -> scan gone
    started = StationAuditLog.objects.filter(station=station, message__contains="started scan")
    assert started.exists()


@pytest.mark.django_db(transaction=True)
def test_scan_start_requires_lock(control_agent_auth):
    station, user = _setup("sc-lock")
    _rep()
    kind, frame, rows = _run_start(station, user, _start(), acquire=False)
    assert kind == "error" and _code(frame) == "not_locked" and frame["request_id"] == "s1"
    assert rows == []


@pytest.mark.django_db(transaction=True)
def test_scan_start_empty_list_refused(control_agent_auth):
    # Review Focus 3: nothing in radius and the list only holds another band.
    station, user = _setup("sc-empty")
    _rep(near=False)
    uhf_only = _list_with_freq(user, freq="433.500")
    kind, frame, rows = _run_start(station, user, _start(list_ids=[uhf_only.pk]))
    assert kind == "error" and _code(frame) == "empty_scan_list"
    assert rows == []


@pytest.mark.django_db(transaction=True)
def test_scan_start_no_coords_radius_refused(control_agent_auth):
    # Review Focus 1: no 500, a clear error; lists alone still work without coordinates.
    station, user = _setup("sc-nocoord", coords=False)
    _rep()
    kind, frame, rows = _run_start(station, user, _start())
    assert kind == "error" and _code(frame) == "no_coordinates"
    calling = _list_with_freq(user)
    StationModule.objects.update(online=True)  # the previous run's agent disconnect
    kind, frame, rows = _run_start(station, user, _start(radius_km=None, list_ids=[calling.pk]))
    assert kind == "relayed" and len(frame["entries"]) == 1, frame


@pytest.mark.django_db(transaction=True)
def test_scan_start_forbidden_without_write(control_agent_auth, monkeypatch):
    station, user = _setup("sc-forbid")
    _rep()
    real = capability_policy.can_write
    monkeypatch.setattr(
        capability_policy,
        "can_write",
        lambda u, cap, mt: False if cap == "tx_tone" else real(u, cap, mt),
    )
    kind, frame, rows = _run_start(station, user, _start())
    assert kind == "error" and _code(frame) == "forbidden"
    assert rows == []


@pytest.mark.django_db(transaction=True)
def test_scan_start_malformed_refused(control_agent_auth):
    station, user = _setup("sc-bad")
    _rep()
    kind, frame, rows = _run_start(station, user, _start(list_ids=["x"]))
    assert kind == "error" and _code(frame) == "invalid_scan"
    kind, frame, rows = _run_start(station, user, _start(radius_km=None))  # no source ticked
    assert kind == "error" and _code(frame) == "invalid_scan"


@pytest.mark.django_db(transaction=True)
def test_scan_start_ignores_foreign_lists(control_agent_auth):
    station, user = _setup("sc-foreign")
    other = User.objects.create(username="u-other")
    foreign = _list_with_freq(other)
    kind, frame, rows = _run_start(station, user, _start(radius_km=None, list_ids=[foreign.pk]))
    assert kind == "error" and _code(frame) == "empty_scan_list"


def test_parse_start_clamps_and_validates():
    p = scan.parse_start(_start(listen_s=0, hang_s=10_000, list_ids=[3, 3, 4]))
    assert p["listen_s"] == scan.LISTEN_RANGE_S[0] and p["hang_s"] == scan.HANG_RANGE_S[1]
    assert p["list_ids"] == [3, 4]
    assert scan.parse_start(_start(listen_s="x"))["listen_s"] == scan.DEFAULT_LISTEN_S
    assert scan.parse_start(_start(radius_km=-1)) is None
    assert scan.parse_start(_start(radius_km=float("nan"))) is None
    assert scan.parse_start(_start(list_ids=[True])) is None
    assert scan.parse_start(_start(band_map={"vhf": 1}))["band_map"] == {"vhf": "1"}
    assert scan.parse_start(_start(band_map={"vhf": [1]})) is None


# -- scan_control ----------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_scan_control_requires_holder(control_agent_auth):
    station, user = _setup("sc-ctl-lock")

    async def scenario():
        agent, browser = await _connect(station, user, acquire=False)
        await browser.send_json_to({"type": "scan_control", "request_id": "c1", "action": "skip"})
        err = await _until(browser, lambda m: m.get("type") == "error")
        assert await agent.receive_nothing(timeout=0.3)
        await browser.disconnect()
        await agent.disconnect()
        return err

    assert _code(asyncio.run(scenario())) == "not_locked"


@pytest.mark.django_db(transaction=True)
def test_scan_control_relays_and_stop_deletes(control_agent_auth):
    station, user = _setup("sc-ctl")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        await browser.send_json_to({"type": "scan_control", "request_id": "c1", "action": "skip"})
        skip = await _until(agent, lambda m: m.get("type") == "scan_control")
        await browser.send_json_to({"type": "scan_control", "request_id": "c2", "action": "bad"})
        bad = await _until(browser, lambda m: m.get("type") == "error")
        await browser.send_json_to({"type": "scan_control", "request_id": "c3", "action": "stop"})
        stop = await _until(agent, lambda m: m.get("type") == "scan_control")
        left = await database_sync_to_async(ActiveScan.objects.count)()
        await browser.send_json_to({"type": "scan_control", "request_id": "c4", "action": "hold"})
        none = await _until(browser, lambda m: m.get("type") == "error")
        await browser.disconnect()
        await agent.disconnect()
        return start, skip, bad, stop, left, none

    start, skip, bad, stop, left, none = asyncio.run(scenario())
    assert skip == {"v": 1, "type": "scan_control", "scan_id": start["scan_id"], "action": "skip"}
    assert _code(bad) == "invalid_action"
    assert stop["action"] == "stop" and stop["scan_id"] == start["scan_id"]
    assert left == 0
    assert _code(none) == "no_active_scan"
    assert StationAuditLog.objects.filter(
        station=station, message="scan stopped (operator)"
    ).exists()


# -- lock changes hands (Review Focus 5) ----------------------------------------


def _lock_stop_scenario(station, user, action):
    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        if action == "release":
            await browser.send_json_to({"type": "lock_release"})
        elif action == "transfer":
            other = await database_sync_to_async(User.objects.create)(
                username=f"to-{station.name}", membership_level=User.MembershipLevel.MEMBER
            )
            await browser.send_json_to({"type": "lock_transfer", "to_user_id": other.pk})
        elif action == "preempt":
            boss = await database_sync_to_async(User.objects.create)(
                username=f"boss-{station.name}",
                membership_level=User.MembershipLevel.ADMIN,
            )
            boss_browser = _browser(boss, station.id)
            assert (await boss_browser.connect())[0] is True
            await boss_browser.send_json_to({"type": "lock_preempt"})
        stop = await _until(agent, lambda m: m.get("type") == "scan_control")
        left = await database_sync_to_async(ActiveScan.objects.count)()
        if action == "preempt":
            await boss_browser.disconnect()
        await browser.disconnect()
        await agent.disconnect()
        return start, stop, left

    return asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["release", "transfer", "preempt"])
def test_lock_change_stops_scan(control_agent_auth, action):
    station, user = _setup(f"sc-{action}")
    _rep()
    start, stop, left = _lock_stop_scenario(station, user, action)
    assert stop == {"v": 1, "type": "scan_control", "scan_id": start["scan_id"], "action": "stop"}
    assert left == 0


@pytest.mark.django_db(transaction=True)
def test_preempting_own_lock_keeps_scan(control_agent_auth):
    station, user = _setup("sc-selfpreempt")
    user.membership_level = User.MembershipLevel.ADMIN
    user.save()
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        await _started(agent, browser, _start())
        await browser.send_json_to({"type": "lock_preempt"})
        await _until(browser, lambda m: m.get("type") == "lock")
        quiet = await agent.receive_nothing(timeout=0.3)
        kept = await database_sync_to_async(ActiveScan.objects.count)()
        await browser.disconnect()
        await agent.disconnect()
        return quiet, kept

    assert asyncio.run(scenario()) == (True, 1)


@pytest.mark.django_db(transaction=True)
def test_grace_expiry_stops_scan(control_agent_auth, monkeypatch):
    from apps.control import constants

    monkeypatch.setattr(constants, "LOCK_SWEEP_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(constants, "RECONNECT_GRACE_SECONDS", 0)
    station, user = _setup("sc-grace")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        await browser.disconnect()  # holder gone -> grace (0 s) -> sweep frees
        stop = await _until(agent, lambda m: m.get("type") == "scan_control")
        left = await database_sync_to_async(ActiveScan.objects.count)()
        await agent.disconnect()
        return start, stop, left

    start, stop, left = asyncio.run(scenario())
    assert stop["action"] == "stop" and stop["scan_id"] == start["scan_id"]
    assert left == 0
    assert ControlLock.objects.get(station=station).holder_id is None


@pytest.mark.django_db
def test_idle_sweep_skipped_while_scanning():
    station, user = _setup("sc-idle")
    assert lock.acquire(station, user) is True
    long_ago = timezone.now() - timedelta(hours=1)
    ControlLock.objects.filter(station=station).update(last_activity=long_ago)
    ActiveScan.objects.create(station=station, user=user, scan_id="x", listen_s=2, hang_s=5)
    assert lock.sweep_lock(station, now=timezone.now(), idle_seconds=300) is False
    # The scan counted as activity: once it ends on its own, no immediate idle release.
    assert ControlLock.objects.get(station=station).last_activity > long_ago
    assert scan.end_scan(station, "no_reachable_entries") is not None
    assert lock.sweep_lock(station, now=timezone.now(), idle_seconds=300) is False
    ActiveScan.objects.create(station=station, user=user, scan_id="y", listen_s=2, hang_s=5)
    # Holder's browser gone: the reconnect grace still frees the lock.
    ControlLock.objects.filter(station=station).update(
        pending_release_at=timezone.now() - timedelta(seconds=1)
    )
    assert lock.sweep_lock(station, now=timezone.now(), idle_seconds=300) is True


@pytest.mark.django_db
def test_idle_sweep_releases_without_scan():
    station, user = _setup("sc-idle2")
    lock.acquire(station, user)
    ControlLock.objects.filter(station=station).update(
        last_activity=timezone.now() - timedelta(hours=1)
    )
    assert lock.sweep_lock(station, now=timezone.now(), idle_seconds=300) is True


# -- agent scan_state -------------------------------------------------------------


def _scan_state(scan_id, state, slot=1, module="fm0", **kw):
    return {
        "v": 1,
        "type": "scan_state",
        "scan_id": scan_id,
        "state": state,
        "index": 0,
        "ref": "r:1",
        "slot": slot,
        "module": module,
        "ts": 1.0,
        **kw,
    }


@pytest.mark.django_db(transaction=True)
def test_scan_state_broadcast_and_tx_route(control_agent_auth):
    station, user = _setup("sc-state")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        await agent.send_json_to(_scan_state(start["scan_id"], "active"))
        seen = await _until(browser, lambda m: m.get("type") == "scan_state")
        gate = await database_sync_to_async(audio_gate.get_state)(station)
        await browser.disconnect()
        await agent.disconnect()
        return seen, gate

    seen, gate = asyncio.run(scenario())
    assert seen["state"] == "active" and seen["module"] == "fm0"
    assert (gate["tx_slot"], gate["tx_module"]) == (1, "fm0")


@pytest.mark.django_db(transaction=True)
def test_stale_scan_state_does_not_move_tx_route(control_agent_auth):
    station, user = _setup("sc-stale")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        await agent.send_json_to(_scan_state("old", "held", slot=2, module="fm9"))
        await agent.send_json_to(_scan_state("old", "stopped", reason="replaced"))
        await _until(
            browser, lambda m: m.get("type") == "scan_state" and m.get("state") == "stopped"
        )
        gate = await database_sync_to_async(audio_gate.get_state)(station)
        ids = await database_sync_to_async(
            lambda: list(ActiveScan.objects.values_list("scan_id", flat=True))
        )()
        await browser.disconnect()
        await agent.disconnect()
        return start, gate, ids

    start, gate, ids = asyncio.run(scenario())
    assert gate["tx_module"] != "fm9"
    # A late "stopped" of a replaced scan must not delete the running one.
    assert ids == [start["scan_id"]]


@pytest.mark.django_db(transaction=True)
def test_stopped_state_deletes_active_scan(control_agent_auth):
    station, user = _setup("sc-stopped")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        await agent.send_json_to(
            _scan_state(start["scan_id"], "stopped", slot=None, module=None, reason="ptt")
        )
        await _until(browser, lambda m: m.get("type") == "scan_state")
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    assert not ActiveScan.objects.exists()
    assert StationAuditLog.objects.filter(station=station, message="scan stopped (ptt)").exists()


@pytest.mark.django_db(transaction=True)
def test_scan_steps_not_persisted(control_agent_auth):
    station, user = _setup("sc-steps")
    _rep()
    calling = _list_with_freq(user)

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start(list_ids=[calling.pk]))
        for freq in (145.5, 145.6):
            await agent.send_json_to(
                {
                    "v": 1,
                    "type": "state",
                    "slot": 1,
                    "module": "fm0",
                    "values": {"rx_frequency": freq, "tx_frequency": freq},
                    "ts": 1.0,
                }
            )
            await agent.send_json_to(_scan_state(start["scan_id"], "scanning"))
        await _until(browser, lambda m: m.get("type") == "scan_state")
        await asyncio.sleep(0.2)
        running = await database_sync_to_async(ActiveScan.objects.count)()
        await browser.disconnect()
        await agent.disconnect()
        return running

    assert asyncio.run(scenario()) == 1  # steps don't end the scan
    assert not PersistedCapability.objects.filter(station=station).exists()
    assert not StationAuditLog.objects.filter(station=station, message__contains="145.6").exists()


@pytest.mark.django_db(transaction=True)
def test_agent_disconnect_drops_active_scan(control_agent_auth):
    station, user = _setup("sc-agentgone")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        await _started(agent, browser, _start())
        await agent.disconnect()
        await _until(browser, lambda m: m.get("type") == "agent_offline")
        await browser.disconnect()

    asyncio.run(scenario())
    assert not ActiveScan.objects.exists()


# -- list edits -> scan_update ------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_list_edit_pushes_scan_update(control_agent_auth):
    station, user = _setup("sc-update")
    calling = _list_with_freq(user)

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start(radius_km=None, list_ids=[calling.pk]))
        await database_sync_to_async(ScanListEntry.objects.create)(
            scan_list=calling, position=1, frequency=Decimal("145.525"), tone="77.0"
        )
        update = await _until(agent, lambda m: m.get("type") == "scan_update")
        await database_sync_to_async(calling.delete)()  # cascade: one push per commit
        emptied = await _until(agent, lambda m: m.get("type") == "scan_update")
        extra = await agent.receive_nothing(timeout=0.3)
        await browser.disconnect()
        await agent.disconnect()
        return start, update, emptied, extra

    start, update, emptied, extra = asyncio.run(scenario())
    assert update["scan_id"] == start["scan_id"] and update["v"] == 1
    assert [e["rx_frequency"] for e in update["entries"]] == [145.5, 145.525]
    assert update["entries"][1]["tx_tone"] == "77.0" and update["entries"][1]["slot"] == 1
    assert emptied["entries"] == []  # the agent stops with empty_scan_list
    assert extra is True


@pytest.mark.django_db
def test_edit_of_unrelated_list_pushes_nothing(monkeypatch):
    station, user = _setup("sc-unrelated")
    used, other = _list_with_freq(user, name="used"), _list_with_freq(user, name="other")
    ActiveScan.objects.create(
        station=station, user=user, scan_id="x", list_ids=[used.pk], listen_s=2, hang_s=5
    )
    sent = []
    monkeypatch.setattr(scan, "_send_to_agent", lambda pk, frame: sent.append(frame))
    scan.push_scan_updates({other.pk})
    assert sent == []
    scan.push_scan_updates({used.pk})
    assert [f["type"] for f in sent] == ["scan_update"]


@pytest.mark.django_db(transaction=True)
def test_agent_connect_drops_stale_active_scan(control_agent_auth):
    # Server restart / superseded socket: a row nobody ends would block the idle sweep.
    station, user = _setup("sc-stale-row")
    ActiveScan.objects.create(station=station, user=user, scan_id="old", listen_s=2, hang_s=5)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        left = await database_sync_to_async(ActiveScan.objects.count)()
        await agent.disconnect()
        return left

    assert asyncio.run(scenario()) == 0
    assert StationAuditLog.objects.filter(
        station=station, message="scan stopped (agent reconnect)"
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_rolled_back_list_edit_is_not_pushed_later(monkeypatch):
    from django.db import transaction

    station, user = _setup("sc-rollback")
    rolled, other = _list_with_freq(user, name="rolled"), _list_with_freq(user, name="other")
    ActiveScan.objects.create(
        station=station, user=user, scan_id="x", list_ids=[rolled.pk], listen_s=2, hang_s=5
    )
    pushed = []
    monkeypatch.setattr(scan, "push_scan_updates", lambda ids: pushed.append(set(ids)))

    class AbortError(Exception):
        pass

    with pytest.raises(AbortError):
        with transaction.atomic():
            ScanListEntry.objects.create(scan_list=rolled, position=1, frequency=Decimal("145.3"))
            raise AbortError
    assert pushed == []
    with transaction.atomic():
        ScanListEntry.objects.create(scan_list=other, position=1, frequency=Decimal("145.4"))
        ScanListEntry.objects.create(scan_list=other, position=2, frequency=Decimal("145.45"))
    assert pushed == [{other.pk}]  # one push per commit, without the rolled-back list


# -- review #192 round 1 -------------------------------------------------------------


@pytest.mark.django_db
def test_only_scanning_or_fresh_start_counts_as_activity():
    station, user = _setup("sc-act")
    row = ActiveScan.objects.create(station=station, user=user, scan_id="x", listen_s=2, hang_s=5)
    assert scan.scan_counts_as_activity(station)  # fresh, unconfirmed start
    scan.note_state(station, "x", "held")
    assert not scan.scan_counts_as_activity(station)  # parked scan idles like a radio
    scan.note_state(station, "other", "active")  # another scan_id changes nothing
    assert ActiveScan.objects.get(pk=row.pk).state == "held"
    scan.note_state(station, "x", "active")
    assert scan.scan_counts_as_activity(station)
    scan.note_state(station, "x", "scanning")  # only active/held are stored
    assert ActiveScan.objects.get(pk=row.pk).state == "active"
    # An agent that never confirms (ignores scan_start) keeps the lock only briefly.
    ActiveScan.objects.filter(pk=row.pk).update(
        state="starting",
        started_at=timezone.now() - timedelta(seconds=scan.START_GRACE_S + 1),
    )
    assert not scan.scan_counts_as_activity(station)


@pytest.mark.django_db
def test_held_scan_lets_the_lock_idle_out():
    station, user = _setup("sc-heldidle")
    lock.acquire(station, user)
    ControlLock.objects.filter(station=station).update(
        last_activity=timezone.now() - timedelta(hours=1)
    )
    ActiveScan.objects.create(
        station=station, user=user, scan_id="x", listen_s=2, hang_s=5, state="held"
    )
    assert lock.sweep_lock(station, now=timezone.now(), idle_seconds=300) is True


@pytest.mark.django_db(transaction=True)
def test_scan_state_stores_coarse_state(control_agent_auth):
    station, user = _setup("sc-coarse")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        await agent.send_json_to(_scan_state(start["scan_id"], "held", reason="manual"))
        await _until(browser, lambda m: m.get("type") == "scan_state")
        state = await database_sync_to_async(
            lambda: ActiveScan.objects.values_list("state", flat=True).get()
        )()
        await browser.disconnect()
        await agent.disconnect()
        return state

    assert asyncio.run(scenario()) == "held"


@pytest.mark.django_db(transaction=True)
def test_held_scan_state_never_moves_mic_off_a_keyed_module(control_agent_auth):
    station, user = _setup("sc-keyed")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        await database_sync_to_async(audio_gate.set_tx_route)(station, 2, "fm9")
        await database_sync_to_async(audio_gate.set_ptt)(station, 2, "fm9", ttl=30)
        await agent.send_json_to(_scan_state(start["scan_id"], "held", reason="ptt"))
        await _until(browser, lambda m: m.get("type") == "scan_state")
        gate = await database_sync_to_async(audio_gate.get_state)(station)
        await database_sync_to_async(audio_gate.clear_ptt)(station)
        await browser.disconnect()
        await agent.disconnect()
        return gate

    gate = asyncio.run(scenario())
    assert (gate["tx_slot"], gate["tx_module"]) == (2, "fm9")


@pytest.mark.django_db(transaction=True)
def test_agent_disconnect_tells_browsers_the_scan_stopped(control_agent_auth):
    station, user = _setup("sc-gonebc")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        await agent.disconnect()
        stopped = await _until(browser, lambda m: m.get("type") == "scan_state")
        await browser.disconnect()
        return start, stopped

    start, stopped = asyncio.run(scenario())
    assert stopped["scan_id"] == start["scan_id"]
    assert (stopped["state"], stopped["reason"]) == ("stopped", "disconnect")


@pytest.mark.django_db(transaction=True)
def test_browser_connect_gets_running_scan_snapshot(control_agent_auth):
    station, user = _setup("sc-snap")
    _rep()

    async def scenario():
        agent, browser = await _connect(station, user)
        start = await _started(agent, browser, _start())
        late = _browser(user, station.id)
        assert (await late.connect())[0] is True
        snap = await _until(late, lambda m: m.get("type") == "scan_state")
        await late.disconnect()
        await browser.disconnect()
        await agent.disconnect()
        return start, snap

    start, snap = asyncio.run(scenario())
    assert snap["scan_id"] == start["scan_id"]
    assert snap["state"] == "active" and snap["index"] is None
