import asyncio
import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator

from apps.passive.coverage import cell_of
from apps.passive.ingest import ingest_output, server_heard_at
from apps.passive.models import AprsPacket, CoverageCell, PassiveServiceRun
from apps.stations.models import Station
from config.asgi import application

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _addr(call: str, ssid: int = 0, last: bool = False, h: bool = False) -> bytes:
    b = bytearray(ord(c) << 1 for c in call.ljust(6))
    b.append(0x60 | (ssid << 1) | (0x80 if h else 0) | (1 if last else 0))
    return bytes(b)


def _frame(src="OE1ABC", info=b"!4800.00N/01400.00E-test", digi=None) -> str:
    raw = _addr("APRS") + _addr(src, last=digi is None)
    if digi:
        raw += _addr(digi, last=True, h=True)
    raw += bytes([0x03, 0xF0]) + info
    return base64.b64encode(raw).decode()


def _msg(seq=1, epoch="e1", ax25=None, **kw):
    m = {
        "v": 1,
        "type": "decoder_output",
        "decoder": "aprs",
        "slot": 0,
        "epoch": epoch,
        "seq": seq,
        "kind": "ax25",
        "reliable": True,
        "data": {"ax25": ax25 or _frame(), "heard_at": 100.0},
        "ts": 100.0,
    }
    m.update(kw)
    return m


@pytest.fixture
def open_run(station):
    return PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="slot0", state="running"
    )


def test_heard_at_uses_agent_clock_delta_only():
    assert server_heard_at(NOW, ts=5000.0, agent_heard_at=4990.0) == NOW - timedelta(seconds=10)
    assert server_heard_at(NOW, ts=10.0, agent_heard_at=50.0) == NOW
    assert server_heard_at(NOW, ts=1e9, agent_heard_at=0.0) == NOW - timedelta(days=7)


def test_ingest_stores_parsed_packet_and_updates_run(db, station, open_run):
    assert ingest_output(station.id, _msg(), recv_now=NOW) is True
    p = AprsPacket.objects.get()
    assert (p.source, p.destination, p.packet_type) == ("OE1ABC", "APRS", "position")
    assert p.run_id == open_run.id and p.digipeated is False
    assert float(p.latitude) == pytest.approx(48.0)
    open_run.refresh_from_db()
    assert open_run.packet_count == 1 and open_run.last_packet_at == NOW


def test_duplicate_seq_acked_not_stored_twice(db, station, open_run):
    assert ingest_output(station.id, _msg(), recv_now=NOW)
    assert ingest_output(station.id, _msg(), recv_now=NOW) is True
    assert AprsPacket.objects.count() == 1
    open_run.refresh_from_db()
    assert open_run.packet_count == 1


def test_bad_ax25_acked_not_stored(db, station):
    assert ingest_output(station.id, _msg(ax25="!!notbase64"), recv_now=NOW) is True
    assert ingest_output(station.id, _msg(seq=2, ax25="AAAA"), recv_now=NOW) is True
    assert AprsPacket.objects.count() == 0


@pytest.mark.parametrize("seq", [None, -1, "3", True])
def test_missing_seq_not_acked(db, station, seq):
    assert ingest_output(station.id, _msg(seq=seq), recv_now=NOW) is False
    assert ingest_output(station.id, _msg(epoch=None), recv_now=NOW) is False
    assert AprsPacket.objects.count() == 0


def test_other_kind_ignored_but_acked(db, station):
    assert ingest_output(station.id, _msg(kind="log"), recv_now=NOW) is True
    assert AprsPacket.objects.count() == 0


def test_direct_position_updates_coverage(db, station):
    station.latitude, station.longitude = Decimal("48.0"), Decimal("13.0")
    station.save()
    ingest_output(station.id, _msg(seq=1), recv_now=NOW)
    ingest_output(station.id, _msg(seq=2), recv_now=NOW + timedelta(seconds=5))
    c = CoverageCell.objects.get()
    assert (c.cell_lat, c.cell_lon) == cell_of(48.0, 14.0)
    assert c.count == 2 and c.max_distance_km > 70
    assert c.first_heard == NOW and c.last_heard == NOW + timedelta(seconds=5)


def test_no_station_coordinates_distance_zero(db, station):
    ingest_output(station.id, _msg(), recv_now=NOW)
    assert CoverageCell.objects.get().max_distance_km == 0


def test_digipeated_excluded_from_coverage(db, station):
    ingest_output(station.id, _msg(ax25=_frame(digi="WIDE1")), recv_now=NOW)
    assert AprsPacket.objects.get().digipeated is True
    assert CoverageCell.objects.count() == 0


def test_object_excluded_from_coverage(db, station):
    obj = b";OBJECT   *111111z4800.00N/01400.00E-"
    ingest_output(station.id, _msg(ax25=_frame(info=obj)), recv_now=NOW)
    assert AprsPacket.objects.get().packet_type == "object"
    assert CoverageCell.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_consumer_acks_after_ingest(control_agent_auth):
    station = Station.objects.create(name="ing1", status="online")

    async def scenario():
        agent = WebsocketCommunicator(
            application, f"/ws/agent/control/{station.id}/?signature=x&timestamp=0"
        )
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_msg(seq=7, epoch="eX"))
        for _ in range(8):
            frame = await agent.receive_json_from()
            if frame.get("type") == "decoder_ack":
                assert frame["epoch"] == "eX" and frame["seqs"] == [7]
                break
        else:
            raise AssertionError("no decoder_ack")
        await agent.disconnect()

    asyncio.run(scenario())
    assert AprsPacket.objects.filter(station=station, seq=7).count() == 1


def test_nul_byte_frame_acked_not_stored(db, station):
    assert ingest_output(station.id, _msg(ax25=_frame(info=b"!x\x00y")), recv_now=NOW) is True
    assert AprsPacket.objects.count() == 0


def test_seq_beyond_bigint_not_acked(db, station):
    assert ingest_output(station.id, _msg(seq=2**63), recv_now=NOW) is False
    assert ingest_output(station.id, _msg(seq=2**63 - 1), recv_now=NOW) is True
