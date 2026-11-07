"""End-to-end repeater list & scanner flow (repeater-scanner plan Task 11): one browser and
one agent socket through the real ASGI stack — lock → ``apply_repeater`` → agent
``apply_preset`` → ``result`` back to the browser; then ``scan_start`` with an own list →
agent ``scan_start`` → ``scan_state`` (enriched with label/frequency/count) back to the
browser → ``stopped`` drops the ``ActiveScan``. Async scenarios run via asyncio.run()."""

import asyncio
from decimal import Decimal

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.audio import gate as audio_gate
from apps.control.models import ActiveScan, StationModule
from apps.repeaters.models import Repeater, ScanList, ScanListEntry
from apps.stations.models import Station, StationAuditLog
from config.asgi import application

VHF = {"name": "vhf", "min": 134.0, "max": 174.0}


def _desc():
    return [
        {"name": "tx_frequency", "kind": "setting", "type": "float", "ranges": [VHF]},
        {"name": "rx_frequency", "kind": "setting", "type": "float", "ranges": [VHF]},
        {"name": "tx_tone", "kind": "setting", "type": "string"},
        {"name": "rx_tone", "kind": "setting", "type": "string"},
        {"name": "carrier", "kind": "telemetry", "type": "bool", "readonly": True},
    ]


def _setup():
    station = Station.objects.create(
        name="e2e-rs", status="online", latitude=Decimal("48.0"), longitude=Decimal("14.0")
    )
    user = User.objects.create(username="u-e2e-rs", membership_level=User.MembershipLevel.MEMBER)
    StationModule.objects.create(
        station=station,
        slot="1",
        module_id="fm0",
        type="fm_transceiver",
        online=True,
        capability_descriptor=_desc(),
    )
    rep = Repeater.objects.create(
        callsign="OE5XLL",
        output_frequency=Decimal("145.600"),
        input_frequency=Decimal("145.000"),
        input_tone="77.0",
        latitude=Decimal("48.1"),
        longitude=Decimal("14.0"),
        source="manual",
    )
    scan_list = ScanList.objects.create(owner=user, name="mine")
    ScanListEntry.objects.create(
        scan_list=scan_list, position=0, frequency=Decimal("145.500"), label="calling"
    )
    ScanListEntry.objects.create(scan_list=scan_list, position=1, repeater=rep)
    return station, user, rep, scan_list


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


def _of(mtype):
    return lambda m: m.get("type") == mtype


@pytest.mark.django_db(transaction=True)
def test_repeater_apply_and_scan_end_to_end(control_agent_auth):
    station, user, rep, scan_list = _setup()
    rows = database_sync_to_async(lambda: list(ActiveScan.objects.filter(station=station)))

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        await browser.send_json_to({"type": "lock_acquire"})
        await _until(browser, lambda m: m.get("type") == "lock" and m.get("state") == "held")

        # -- apply_repeater -> apply_preset -> result ------------------------------
        await browser.send_json_to(
            {"type": "apply_repeater", "request_id": "a1", "repeater_id": rep.pk, "slot": "1"}
        )
        preset = await _until(agent, _of("apply_preset"))
        assert preset["request_id"] == "a1"
        assert (preset["slot"], preset["module"]) == (1, "fm0")
        assert preset["values"]["tx_frequency"] == 145.0
        assert preset["values"]["rx_frequency"] == 145.6
        assert preset["values"]["tx_tone"] == "77.0"
        await agent.send_json_to(
            {
                "v": 1,
                "type": "result",
                "request_id": "a1",
                "ok": True,
                "value": {"tx_frequency": {"ok": True}, "rx_frequency": {"ok": True}},
            }
        )
        result = await _until(browser, _of("result"))
        assert result["request_id"] == "a1" and result["ok"] is True

        # -- scan_start with the own list only (no radius) ------------------------
        await browser.send_json_to(
            {
                "type": "scan_start",
                "request_id": "s1",
                "radius_km": None,
                "list_ids": [scan_list.pk],
                "band_map": {},
                "listen_s": 2,
                "hang_s": 5,
            }
        )
        start = await _until(agent, _of("scan_start"))
        entries = start["entries"]
        assert [e["label"] for e in entries][0] == "calling"
        assert [(e["rx_frequency"], e["tx_frequency"]) for e in entries] == [
            (145.5, 145.5),
            (145.6, 145.0),
        ]
        (row,) = await rows()
        assert row.scan_id == start["scan_id"] and row.user_id == user.pk

        # Agent reports the second entry active -> browser sees it, enriched.
        await agent.send_json_to(
            {
                "v": 1,
                "type": "scan_state",
                "scan_id": start["scan_id"],
                "state": "active",
                "index": 1,
                "ref": entries[1]["ref"],
                "slot": 1,
                "module": "fm0",
                "ts": 1.0,
            }
        )
        active = await _until(browser, _of("scan_state"))
        assert active["state"] == "active" and active["index"] == 1
        assert active["count"] == 2
        assert active["rx_frequency"] == 145.6
        assert active["label"] == entries[1]["label"]
        gate = await database_sync_to_async(audio_gate.get_state)(station)
        assert (gate["tx_slot"], gate["tx_module"]) == (1, "fm0")

        # Agent stops -> browser sees it, the ActiveScan row is gone.
        await agent.send_json_to(
            {
                "v": 1,
                "type": "scan_state",
                "scan_id": start["scan_id"],
                "state": "stopped",
                "index": None,
                "ref": None,
                "slot": None,
                "module": None,
                "ts": 2.0,
                "reason": "operator",
            }
        )
        stopped = await _until(browser, _of("scan_state"))
        assert stopped["state"] == "stopped" and stopped["reason"] == "operator"
        assert await rows() == []

        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    log = StationAuditLog.objects.filter(station=station)
    assert log.filter(message__contains="applied repeater OE5XLL").exists()
    assert log.filter(message__contains="started scan (2 entries)").exists()
    assert log.filter(message__contains="scan stopped (operator)").exists()


def test_scan_state_enrichment_only_for_the_relayed_scan():
    from apps.control.consumers import AgentControlConsumer

    consumer = AgentControlConsumer()
    consumer._scan_index = None
    state = {"type": "scan_state", "scan_id": "s1", "ref": "r:1", "index": 0}
    assert consumer._enrich_scan_state(state) is state  # nothing relayed yet

    consumer._note_scan_entries({"type": "set", "entries": [{"ref": "r:9"}]})  # ignored
    assert consumer._scan_index is None
    consumer._note_scan_entries(
        {
            "type": "scan_start",
            "scan_id": "s1",
            "entries": [{"ref": "r:1", "label": "OE5XLL", "rx_frequency": 145.6}, "junk"],
        }
    )
    assert consumer._enrich_scan_state(state) == {
        **state,
        "count": 2,
        "label": "OE5XLL",
        "rx_frequency": 145.6,
    }
    # Unknown ref: only the total; an agent-sent field always wins.
    assert consumer._enrich_scan_state({**state, "ref": "r:2"}) == {
        **state,
        "ref": "r:2",
        "count": 2,
    }
    assert consumer._enrich_scan_state({**state, "label": "agent"})["label"] == "agent"
    # Another (stale/replaced) scan is left untouched.
    other = {**state, "scan_id": "old"}
    assert consumer._enrich_scan_state(other) is other

    consumer._note_scan_entries({"type": "scan_update", "scan_id": "s1", "entries": []})
    assert consumer._enrich_scan_state(state) == {**state, "count": 0}
