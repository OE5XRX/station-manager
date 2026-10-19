"""Teilbereich D, Task S6: "Firmware-Update läuft" per module + the station drain
banner ("Station wartet auf Firmware-Update seit …").

Server side only — the browser behaviour lives in
tests/js/control-panel-firmware.test.mjs (run via tests/test_control_logic_js.py).
"""

import asyncio
from datetime import UTC, datetime
from unittest import mock

import pytest
from channels.testing import WebsocketCommunicator
from django.urls import reverse

from apps.accounts.models import User
from apps.control.models import StationModule
from apps.module_firmware.drain import DrainStatus
from apps.stations.models import Station
from config.asgi import application

DRAIN_STATUS = "apps.module_firmware.drain.drain_status"
INGEST = "apps.control.registry.ingest_module"
BANNER = "Station wartet auf Firmware-Update seit"
# 2026-10-18 08:05 UTC == 10:05 Europe/Vienna (CEST).
SINCE = datetime(2026, 10, 18, 8, 5, tzinfo=UTC)
ACTIVE = DrainStatus(active=True, since=SINCE, module_uids=("uid-1",))
INACTIVE = DrainStatus(active=False, since=None, module_uids=())

FM_CAPS = [{"name": "frequency", "kind": "setting", "type": "float"}]


def _entry(status=None, version="1.0"):
    mod = {
        "module": "fm0",
        "identity": {"type": "fm", "model": "SA818", "version": version, "uid": "uid-1"},
        "capabilities": FM_CAPS,
        "state": {"frequency": 145.5},
    }
    if status is not None:
        mod["status"] = status
    return [{"slot": "slot0", "modules": [mod]}]


def _member(name="fwop"):
    u = User.objects.create_user(username=name, password="x")
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
def test_stationmodule_firmware_updating_defaults_false():
    station = Station.objects.create(name="fwu0")
    sm = StationModule.objects.create(station=station, slot="slot0", module_id="fm0")
    assert sm.firmware_updating is False


@pytest.mark.django_db
def test_inventory_updating_sets_flag_and_skips_ingest():
    from apps.control import registry

    station = Station.objects.create(name="fwu1")
    StationModule.objects.create(
        station=station, slot="slot0", module_id="fm0", type="fm", version="1.0"
    )
    with mock.patch(INGEST) as ingest:
        # A paused slot reports its last-known entry; the version may be stale
        # or mid-flash garbage — it must not churn the persisted identity.
        registry.apply_inventory(station, _entry(status="updating", version="9.9"))
    ingest.assert_not_called()
    sm = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert sm.firmware_updating is True
    assert sm.online is True
    assert sm.version == "1.0"


@pytest.mark.django_db
def test_inventory_updating_keeps_tracked_link():
    """Skipping ingest must not be mistaken for a rejected entry (link cleared)."""
    from apps.control import registry

    station = Station.objects.create(name="fwu1b")
    registry.apply_inventory(station, _entry())
    sm = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    tracked_id = sm.tracked_module_id
    registry.apply_inventory(station, _entry(status="updating"))
    sm.refresh_from_db()
    assert sm.tracked_module_id == tracked_id
    assert sm.firmware_updating is True


@pytest.mark.django_db
def test_inventory_normal_clears_flag():
    from apps.control import registry

    station = Station.objects.create(name="fwu2")
    StationModule.objects.create(
        station=station, slot="slot0", module_id="fm0", firmware_updating=True
    )
    with mock.patch(INGEST, return_value=None) as ingest:
        registry.apply_inventory(station, _entry())
    ingest.assert_called_once()
    sm = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert sm.firmware_updating is False
    assert sm.online is True


@pytest.mark.django_db
def test_snapshot_includes_updating():
    from apps.control import serializers

    station = Station.objects.create(name="fwu3")
    StationModule.objects.create(
        station=station, slot="slot0", module_id="fm0", firmware_updating=True
    )
    StationModule.objects.create(station=station, slot="slot1", module_id="fm1")
    snap = serializers.snapshot(station)
    by_slot = {s["slot"]: s["modules"][0] for s in snap}
    assert by_slot["slot0"]["updating"] is True
    assert by_slot["slot1"]["updating"] is False


def _agent_comm(station_id):
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


@pytest.mark.django_db(transaction=True)
def test_started_finished_events_toggle_flag(control_agent_auth):
    from channels.layers import get_channel_layer

    station = Station.objects.create(name="fwu4", status="online")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)
    other = StationModule.objects.create(
        station=station, slot="slot1", module_id="fm0", online=True
    )

    def flag():
        return StationModule.objects.get(
            station=station, slot="slot0", module_id="fm0"
        ).firmware_updating

    seen = {}

    async def scenario():
        layer = get_channel_layer()
        spy = "fw-viewer-spy"
        await layer.group_add(f"control_{station.id}", spy)
        agent = _agent_comm(station.id)
        connected, _ = await agent.connect()
        assert connected is True

        await agent.send_json_to(
            {
                "v": 1,
                "type": "event",
                "slot": "slot0",
                "module": "fm0",
                "event": "firmware_update_started",
                "detail": {"target_version": "2.0", "convergence_id": 7},
            }
        )
        evt = await _receive_type(layer, spy, "control.event")
        assert evt["msg"]["event"] == "firmware_update_started"
        seen["after_started"] = await asyncio.to_thread(flag)

        await agent.send_json_to(
            {
                "v": 1,
                "type": "event",
                "slot": "slot0",
                "module": "fm0",
                "event": "firmware_update_finished",
                "detail": {"result": "success", "target_version": "2.0"},
            }
        )
        evt = await _receive_type(layer, spy, "control.event")
        assert evt["msg"]["event"] == "firmware_update_finished"
        seen["after_finished"] = await asyncio.to_thread(flag)
        await agent.disconnect()

    asyncio.run(scenario())
    assert seen == {"after_started": True, "after_finished": False}
    other.refresh_from_db()
    assert other.firmware_updating is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("result", "online_after"), [("failed", True), ("success", False)])
def test_finished_event_result_decides_soft_offline(control_agent_auth, result, online_after):
    """Review #168 verify: the consumer forwards the finished detail's result — a fast
    pre-flash failure (no 'updating' inventory ever sent) leaves the module online; a
    real flash soft-offlines it until the agent's forced post-finish inventory."""
    from channels.layers import get_channel_layer

    station = Station.objects.create(name=f"fwu-res-{result}", status="online")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)

    async def scenario():
        layer = get_channel_layer()
        spy = f"fw-res-spy-{result}"
        await layer.group_add(f"control_{station.id}", spy)
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        for event, detail in (
            ("firmware_update_started", {"target_version": "2.0", "convergence_id": 7}),
            ("firmware_update_finished", {"result": result, "target_version": "2.0"}),
        ):
            await agent.send_json_to(
                {
                    "v": 1,
                    "type": "event",
                    "slot": "slot0",
                    "module": "fm0",
                    "event": event,
                    "detail": detail,
                }
            )
            await _receive_type(layer, spy, "control.event")
        # Read before disconnect: mark_station_offline would offline every row.
        sm = await asyncio.to_thread(
            StationModule.objects.get, station=station, slot="slot0", module_id="fm0"
        )
        await agent.disconnect()
        return sm

    sm = asyncio.run(scenario())
    assert sm.firmware_updating is False
    assert sm.online is online_after


@pytest.mark.django_db
def test_station_detail_shows_drain_banner(client):
    station = Station.objects.create(name="fwu5", status="online")
    client.force_login(_member("fwdet"))
    with mock.patch(DRAIN_STATUS, return_value=ACTIVE):
        body = client.get(reverse("stations:station_detail", args=[station.pk])).content.decode()
    assert f"{BANNER} 18.10.2026 10:05" in body


@pytest.mark.django_db
def test_station_detail_hides_drain_banner_when_inactive(client):
    station = Station.objects.create(name="fwu6", status="online")
    client.force_login(_member("fwdet2"))
    with mock.patch(DRAIN_STATUS, return_value=INACTIVE):
        body = client.get(reverse("stations:station_detail", args=[station.pk])).content.decode()
    assert BANNER not in body


@pytest.mark.django_db
def test_control_panel_shows_drain_banner(client):
    station = Station.objects.create(name="fwu7", status="online")
    StationModule.objects.create(
        station=station, slot="slot0", module_id="fm0", type="fm", online=True
    )
    client.force_login(_member("fwpanel"))
    with mock.patch(DRAIN_STATUS, return_value=ACTIVE):
        body = client.get(reverse("control:station_control", args=[station.pk])).content.decode()
    assert f"{BANNER} 18.10.2026 10:05" in body
    # The live component seeds its state from the same value (lock frames update it).
    assert f'data-drain-since="{SINCE.isoformat()}"' in body


@pytest.mark.django_db
def test_control_panel_drain_inactive_seeds_empty(client):
    station = Station.objects.create(name="fwu8", status="online")
    client.force_login(_member("fwpanel2"))
    with mock.patch(DRAIN_STATUS, return_value=INACTIVE):
        body = client.get(reverse("control:station_control", args=[station.pk])).content.decode()
    assert 'data-drain-since=""' in body
    assert "18.10.2026" not in body


@pytest.mark.django_db
def test_control_panel_card_renders_updating_banner(client):
    station = Station.objects.create(name="fwu9", status="online")
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm0",
        type="fm",
        online=True,
        firmware_updating=True,
    )
    client.force_login(_member("fwcard"))
    with mock.patch(DRAIN_STATUS, return_value=INACTIVE):
        body = client.get(reverse("control:station_control", args=[station.pk])).content.decode()
    assert "Firmware-Update läuft" in body
    # First paint already shows the updating state (no x-cloak on its strip).
    assert "cp-module-card is-updating" in body


@pytest.mark.django_db
def test_control_panel_card_not_updating_by_default(client):
    station = Station.objects.create(name="fwu10", status="online")
    StationModule.objects.create(
        station=station, slot="slot0", module_id="fm0", type="fm", online=True
    )
    client.force_login(_member("fwcard2"))
    with mock.patch(DRAIN_STATUS, return_value=INACTIVE):
        body = client.get(reverse("control:station_control", args=[station.pk])).content.decode()
    assert "cp-module-card is-updating" not in body


async def _receive_type(layer, channel, msg_type, tries=6):
    for _ in range(tries):
        evt = await asyncio.wait_for(layer.receive(channel), timeout=2)
        if evt["type"] == msg_type:
            return evt
    raise AssertionError(f"never saw {msg_type}")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("event", "drain", "expected"),
    [
        ("firmware_update_started", ACTIVE, {"since": SINCE.isoformat()}),
        ("firmware_update_finished", INACTIVE, None),
    ],
)
def test_firmware_events_rebroadcast_lock_with_fresh_drain(
    control_agent_auth, event, drain, expected
):
    """An open panel's drain banner follows the flash without a page reload."""
    from channels.layers import get_channel_layer

    station = Station.objects.create(name=f"fwu-lock-{event}", status="online")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)

    async def scenario():
        layer = get_channel_layer()
        spy = f"fw-lock-spy-{event}"
        await layer.group_add(f"control_{station.id}", spy)
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        with mock.patch(DRAIN_STATUS, return_value=drain):
            await agent.send_json_to(
                {"v": 1, "type": "event", "slot": "slot0", "module": "fm0", "event": event}
            )
            evt = await _receive_type(layer, spy, "control.lock")
        await agent.disconnect()
        return evt

    evt = asyncio.run(scenario())
    assert evt["lock"]["drain"] == expected


@pytest.mark.django_db(transaction=True)
def test_firmware_check_nudge_rebroadcasts_lock(control_agent_auth):
    from channels.layers import get_channel_layer

    from apps.module_firmware.nudge import FIRMWARE_CHECK_FRAME

    station = Station.objects.create(name="fwu-lock-nudge", status="online")

    async def scenario():
        layer = get_channel_layer()
        spy = "fw-lock-spy-nudge"
        await layer.group_add(f"control_{station.id}", spy)
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        with mock.patch(DRAIN_STATUS, return_value=ACTIVE):
            await layer.group_send(
                f"control_{station.id}_agent",
                {"type": "control.to_agent", "frame": dict(FIRMWARE_CHECK_FRAME)},
            )
            frame = await agent.receive_json_from(timeout=2)
            evt = await _receive_type(layer, spy, "control.lock")
        await agent.disconnect()
        return frame, evt

    frame, evt = asyncio.run(scenario())
    assert frame["type"] == "firmware_check"
    assert evt["lock"]["drain"] == {"since": SINCE.isoformat()}


def _inventory_drain_scenario(station, drain_after, calls=None):
    """Agent sends an inventory; drain flips from ACTIVE to ``drain_after`` while the
    inventory is applied. Returns the control.* event types the viewer saw after it.
    ``calls`` (list) collects one entry per drain-predicate evaluation."""
    from channels.layers import get_channel_layer

    state = {"drain": ACTIVE}
    calls = [] if calls is None else calls

    def drain_status(st):
        calls.append(st)
        return state["drain"]

    def apply(station_, slots):
        state["drain"] = drain_after

    async def scenario():
        layer = get_channel_layer()
        spy = f"fw-inv-spy-{station.pk}"
        await layer.group_add(f"control_{station.id}", spy)
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        with (
            mock.patch(DRAIN_STATUS, side_effect=drain_status),
            mock.patch("apps.control.registry.apply_inventory", side_effect=apply),
        ):
            await agent.send_json_to({"v": 1, "type": "inventory", "slots": _entry()})
            await _receive_type(layer, spy, "control.inventory")
            seen = []
            try:
                while True:
                    seen.append(await asyncio.wait_for(layer.receive(spy), timeout=0.5))
            except TimeoutError:
                pass
        await agent.disconnect()
        return seen

    return asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_inventory_that_ends_drain_rebroadcasts_lock(control_agent_auth):
    """Post-flash inventory converges the row (drain ends): open banners clear."""
    station = Station.objects.create(name="fwu-inv-lock", status="online")
    seen = _inventory_drain_scenario(station, INACTIVE)
    locks = [e for e in seen if e["type"] == "control.lock"]
    assert len(locks) == 1
    assert locks[0]["lock"]["drain"] is None


@pytest.mark.django_db(transaction=True)
def test_inventory_drain_change_evaluates_drain_only_twice(control_agent_auth):
    """The drain predicate runs once before and once after the apply; the lock
    re-broadcast reuses the 'after' payload instead of evaluating it a third time."""
    station = Station.objects.create(name="fwu-inv-once", status="online")
    calls = []
    seen = _inventory_drain_scenario(station, INACTIVE, calls=calls)
    assert len([e for e in seen if e["type"] == "control.lock"]) == 1
    assert len(calls) == 2


@pytest.mark.django_db(transaction=True)
def test_inventory_without_drain_change_sends_no_lock(control_agent_auth):
    station = Station.objects.create(name="fwu-inv-nolock", status="online")
    seen = _inventory_drain_scenario(station, ACTIVE)
    assert [e for e in seen if e["type"] == "control.lock"] == []
