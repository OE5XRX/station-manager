import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.passive.aprs.worker import IgateWorker
from apps.passive.models import (
    AprsPacket,
    AprsSettings,
    CoverageCell,
    PassiveServiceRun,
    StationPassiveConfig,
)
from apps.stations.models import Station

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.django_db(transaction=True)


class FakeWriter:
    def __init__(self):
        self.data = b""
        self.closed = False
        self.fail = False

    def write(self, b):
        if self.fail:
            raise ConnectionResetError
        self.data += b

    async def drain(self):
        if self.fail:
            raise ConnectionResetError

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    @property
    def lines(self):
        return self.data.decode().splitlines()


class Net:
    """Fake ``asyncio.open_connection``: records every login."""

    def __init__(self):
        self.writers: list[FakeWriter] = []
        self.readers: list[asyncio.StreamReader] = []
        self.fail = False
        self.attempts = 0

    async def __call__(self, host, port):
        self.attempts += 1
        if self.fail:
            raise ConnectionRefusedError
        w, r = FakeWriter(), asyncio.StreamReader()
        self.writers.append(w)
        self.readers.append(r)
        return r, w


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def net():
    return Net()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def worker(net, clock):
    return IgateWorker(connect=net, now=clock)


def _station(name, call="OE5XRX", ssid=10, coords=True, running=True):
    st = Station.objects.create(
        name=name,
        latitude=Decimal("48.1") if coords else None,
        longitude=Decimal("14.2") if coords else None,
    )
    if call:
        StationPassiveConfig.objects.create(
            station=st, enabled=True, igate_callsign=call, igate_ssid=ssid
        )
    run = None
    if running:
        run = PassiveServiceRun.objects.create(
            station=st, service="aprs", slot="slot0", state="running"
        )
    return st, run


_seq = iter(range(1, 10_000))


def _packet(st, run, heard_at=T0, raw="OE1ABC>APRS,WIDE1-1*:!4800.00N/01400.00E-hi", **kw):
    head, _, info = raw.partition(":")
    src, _, hdr = head.partition(">")
    dest, *path = hdr.split(",")
    return AprsPacket.objects.create(
        station=st,
        run=run,
        epoch="e",
        seq=next(_seq),
        heard_at=heard_at,
        received_at=heard_at,
        raw_tnc2=raw,
        source=src,
        destination=dest,
        path=path,
        packet_type="position",
        **kw,
    )


def _run(coro):
    return asyncio.run(coro)


def test_logs_in_once_per_running_station_with_call(worker, net):
    _station("a", call="OE5XRX", ssid=10)
    _station("b", call="OE5ABC", ssid=7)

    async def go():
        await worker.step()
        await worker.step()
        await worker.shutdown()

    _run(go())
    assert len(net.writers) == 2
    logins = sorted(w.lines[0].split()[1] for w in net.writers)
    assert logins == ["OE5ABC-7", "OE5XRX-10"]
    assert all(w.lines[0].startswith("user ") and " pass " in w.lines[0] for w in net.writers)


def test_station_without_call_not_logged_in_packets_skipped(worker, net):
    st, run = _station("a", call=None)
    p = _packet(st, run)
    _run(worker.step())
    assert net.writers == []
    p.refresh_from_db()
    assert (p.gated_at, p.gate_skip) == (None, "no_igate_call")


def test_station_without_coordinates_not_logged_in(worker, net):
    st, run = _station("a", coords=False)
    p = _packet(st, run)
    _run(worker.step())
    assert net.writers == []
    p.refresh_from_db()
    assert p.gate_skip == "no_coordinates"


def test_fresh_packet_gated_exactly_once(worker, net):
    st, run = _station("a")
    p = _packet(st, run)

    async def go():
        await worker.step()
        await worker.step()
        await worker.shutdown()

    _run(go())
    lines = net.writers[0].lines
    gated = [ln for ln in lines if ln.startswith("OE1ABC>")]
    assert gated == ["OE1ABC>APRS,WIDE1-1*,qAR,OE5XRX-10:!4800.00N/01400.00E-hi"]
    p.refresh_from_db()
    assert p.gated_at == T0 and p.gate_skip == ""


def test_stale_packet_marked_not_gated(worker, net):
    st, run = _station("a")
    p = _packet(st, run, heard_at=T0 - timedelta(seconds=31))
    _run(worker.step())
    p.refresh_from_db()
    assert p.gate_skip == "stale" and p.gated_at is None
    assert not any(ln.startswith("OE1ABC>") for w in net.writers for ln in w.lines)


def test_nogate_path_marked(worker, net):
    st, run = _station("a")
    p = _packet(st, run, raw="OE1ABC>APRS,TCPIP*:!4800.00N/01400.00E-hi")
    _run(worker.step())
    p.refresh_from_db()
    assert p.gate_skip == "nogate_path" and p.gated_at is None


def test_connection_closed_when_run_ends(worker, net):
    st, run = _station("a")

    async def go():
        await worker.step()
        from asgiref.sync import sync_to_async

        await sync_to_async(PassiveServiceRun.objects.filter(pk=run.pk).update)(
            ended_at=T0, state="stopped"
        )
        p = await sync_to_async(_packet)(st, run)
        await worker.step()
        return p

    p = _run(go())
    assert net.writers[0].closed is True
    p.refresh_from_db()
    assert p.gate_skip == "not_running"


def test_connect_failure_backs_off(worker, net, clock):
    _station("a")
    net.fail = True

    async def go():
        await worker.step()
        assert net.attempts == 1
        clock.t += timedelta(seconds=10)
        await worker.step()
        assert net.attempts == 1  # still backing off
        clock.t += timedelta(seconds=60)
        await worker.step()
        assert net.attempts == 2
        net.fail = False
        clock.t += timedelta(seconds=600)
        await worker.step()
        await worker.shutdown()

    _run(go())
    assert len(net.writers) == 1  # recovered


def test_packet_not_dropped_while_connect_backs_off(worker, net):
    st, run = _station("a")
    net.fail = True
    p = _packet(st, run)
    _run(worker.step())
    p.refresh_from_db()
    assert p.gate_skip == "" and p.gated_at is None  # may still be gated after reconnect


def test_write_error_closes_connection_and_keeps_packet(worker, net):
    st, run = _station("a")

    async def go():
        await worker.step()
        net.writers[0].fail = True
        from asgiref.sync import sync_to_async

        p = await sync_to_async(_packet)(st, run)
        await worker.step()
        return p

    p = _run(go())
    p.refresh_from_db()
    assert p.gated_at is None and p.gate_skip == ""
    assert net.writers[0].closed is True


def test_beacon_sent_on_login_and_after_interval(worker, net, clock):
    _station("a")

    async def go():
        await worker.step()
        beacons = lambda: [ln for ln in net.writers[0].lines if ln.startswith("OE5XRX-10>APZXRX")]  # noqa: E731
        assert len(beacons()) == 1
        clock.t += timedelta(minutes=29)
        await worker.step()
        assert len(beacons()) == 1
        clock.t += timedelta(minutes=2)
        await worker.step()
        assert len(beacons()) == 2
        await worker.shutdown()

    _run(go())


def test_no_beacon_without_coordinates(worker, net):
    _station("a", coords=False)
    _run(worker.step())
    assert net.writers == []


def test_housekeeping_deletes_only_older_than_retention(worker, clock):
    st, run = _station("a", running=False)
    old = _packet(st, run, heard_at=T0 - timedelta(days=31))
    new = _packet(st, run, heard_at=T0 - timedelta(days=29))
    CoverageCell.objects.create(
        station=st, cell_lat=1, cell_lon=1, count=1, first_heard=T0, last_heard=T0
    )
    assert _run(worker.housekeeping(T0)) == 1
    assert not AprsPacket.objects.filter(pk=old.pk).exists()
    assert AprsPacket.objects.filter(pk=new.pk).exists()
    assert CoverageCell.objects.count() == 1


def test_housekeeping_respects_setting(worker):
    st, run = _station("a", running=False)
    _packet(st, run, heard_at=T0 - timedelta(days=3))
    AprsSettings.objects.update_or_create(pk=1, defaults={"retention_days": 2})
    assert _run(worker.housekeeping(T0)) == 1


async def _mk_old(st, run):
    from asgiref.sync import sync_to_async

    await sync_to_async(_packet)(st, run, heard_at=T0 - timedelta(days=40))


async def _count():
    from asgiref.sync import sync_to_async

    return await sync_to_async(AprsPacket.objects.count)()


def test_step_runs_housekeeping_once_per_day(worker, clock):
    st, run = _station("a", running=False)
    _packet(st, run, heard_at=T0 - timedelta(days=40))

    async def go():
        await worker.step()
        assert await worker.housekeeping(T0) == 0
        await _mk_old(st, run)
        clock.t += timedelta(hours=1)
        await worker.step()  # within 24 h: not run again
        assert await _count() == 1
        clock.t += timedelta(hours=24)
        await worker.step()
        assert await _count() == 0

    _run(go())


def test_command_once(db_station=None):
    call_command("aprs_igate", "--once")


def test_beacon_not_repeated_on_reconnect(worker, net, clock):
    _station("a")

    async def go():
        await worker.step()
        net.writers[0].fail = True
        from asgiref.sync import sync_to_async

        st = await sync_to_async(Station.objects.get)(name="a")
        await sync_to_async(_packet)(st, None)
        await worker.step()  # write error -> conn dropped, backoff
        clock.t += timedelta(seconds=40)
        await worker.step()  # reconnect
        await worker.shutdown()

    _run(go())
    assert len(net.writers) == 2
    beacons = [ln for w in net.writers for ln in w.lines if ln.startswith("OE5XRX-10>APZXRX")]
    assert len(beacons) == 1
