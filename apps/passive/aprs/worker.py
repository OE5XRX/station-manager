"""``aprs_igate`` worker: one APRS-IS login per running station, gating fresh packets from the
DB (RF -> IS only), periodic beacons and raw-packet retention."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.utils import timezone

from ..models import AprsPacket, AprsSettings, PassiveServiceRun, StationPassiveConfig
from .igate import beacon_line, gate_line, login_line

logger = logging.getLogger(__name__)

BATCH = 500
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 10.0
BACKOFF_START_S = 30.0
BACKOFF_MAX_S = 600.0
HOUSEKEEPING_INTERVAL = timedelta(hours=24)
HOUSEKEEPING_BATCH = 5000


@dataclass(frozen=True)
class IgateTarget:
    station_id: int
    call: str
    lat: float | None
    lon: float | None


@dataclass
class _Conn:
    target: IgateTarget
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    last_beacon: datetime | None = None
    closed: bool = False
    drain_task: asyncio.Task | None = field(default=None, repr=False)


def desired_logins() -> dict[str, IgateTarget]:
    """Logins we want: running APRS runs of stations with iGate call and coordinates."""
    out: dict[str, IgateTarget] = {}
    runs = PassiveServiceRun.objects.filter(
        ended_at__isnull=True,
        service="aprs",
        state__in=PassiveServiceRun.RUNNING_STATES,
    ).select_related("station", "station__passive_config")
    for run in runs:
        cfg = getattr(run.station, "passive_config", None)
        call = cfg.igate_call if cfg else None
        st = run.station
        if call and st.latitude is not None and st.longitude is not None:
            out[call] = IgateTarget(st.id, call, float(st.latitude), float(st.longitude))
    return out


def _skip_reasons(station_ids: set[int]) -> dict[int, str]:
    """Why a packet of a station without a login can never be gated."""
    reasons: dict[int, str] = {}
    cfgs = {
        c.station_id: c
        for c in StationPassiveConfig.objects.filter(station_id__in=station_ids).select_related(
            "station"
        )
    }
    for sid in station_ids:
        cfg = cfgs.get(sid)
        if cfg is None or cfg.igate_call is None:
            reasons[sid] = "no_igate_call"
        elif cfg.station.latitude is None or cfg.station.longitude is None:
            reasons[sid] = "no_coordinates"
        else:
            reasons[sid] = "not_running"
    return reasons


class IgateWorker:
    def __init__(
        self,
        *,
        connect: Callable = asyncio.open_connection,
        now: Callable[[], datetime] = timezone.now,
        sleep: Callable = asyncio.sleep,
        version: str = "1",
    ):
        self._connect = connect
        self._now = now
        self._sleep = sleep
        self._version = version
        self._conns: dict[str, _Conn] = {}
        self._backoff: dict[str, tuple[datetime, float]] = {}
        self._last_housekeeping: datetime | None = None

    # -- DB helpers (sync, thread-sensitive) -------------------------------------------

    @staticmethod
    @sync_to_async
    def _load_state():
        close_old_connections()
        own_calls = {
            c.igate_call
            for c in StationPassiveConfig.objects.exclude(igate_callsign="")
            if c.igate_call
        }
        return desired_logins(), AprsSettings.get(), own_calls

    @staticmethod
    @sync_to_async
    def _pending_packets():
        return list(
            AprsPacket.objects.filter(gated_at__isnull=True, gate_skip="").order_by("id")[:BATCH]
        )

    @staticmethod
    @sync_to_async
    def _skip_reasons_for(station_ids):
        return _skip_reasons(set(station_ids))

    @staticmethod
    @sync_to_async
    def _mark(pk: int, *, gated_at=None, gate_skip: str = ""):
        AprsPacket.objects.filter(pk=pk).update(gated_at=gated_at, gate_skip=gate_skip)

    # -- connections -------------------------------------------------------------------

    async def _drain(self, conn: _Conn) -> None:
        """Read and discard server lines; flag the connection dead on EOF/error."""
        warned = False
        try:
            while True:
                line = await conn.reader.readline()
                if not line:
                    break
                text = line.decode("latin-1", "replace").strip()
                if "unverified" in text and not warned:
                    warned = True
                    logger.warning("APRS-IS login for %s unverified: %s", conn.target.call, text)
        except (OSError, asyncio.IncompleteReadError, ValueError):
            pass
        conn.closed = True

    async def _close(self, call: str) -> None:
        conn = self._conns.pop(call, None)
        if conn is None:
            return
        conn.closed = True
        if conn.drain_task is not None:
            conn.drain_task.cancel()
        try:
            conn.writer.close()
            await asyncio.wait_for(conn.writer.wait_closed(), 2.0)
        except (OSError, TimeoutError, asyncio.CancelledError):
            pass

    def _fail(self, call: str, now: datetime) -> None:
        _, delay = self._backoff.get(call, (None, BACKOFF_START_S / 2))
        delay = min(delay * 2, BACKOFF_MAX_S)
        self._backoff[call] = (now + timedelta(seconds=delay), delay)

    async def _write(self, conn: _Conn, line: str) -> bool:
        try:
            conn.writer.write(line.encode("latin-1"))
            await asyncio.wait_for(conn.writer.drain(), WRITE_TIMEOUT_S)
        except (OSError, TimeoutError):
            await self._close(conn.target.call)
            self._fail(conn.target.call, self._now())
            return False
        return True

    async def _login(self, target: IgateTarget, settings: AprsSettings, now: datetime) -> None:
        if (until := self._backoff.get(target.call)) and now < until[0]:
            return
        try:
            reader, writer = await asyncio.wait_for(
                self._connect(settings.aprsis_host, settings.aprsis_port), CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            logger.warning("APRS-IS connect for %s failed: %s", target.call, exc)
            self._fail(target.call, now)
            return
        conn = _Conn(target, reader, writer)
        self._conns[target.call] = conn
        if not await self._write(
            conn, login_line(target.call, settings.aprsis_filter, self._version)
        ):
            return
        conn.drain_task = asyncio.ensure_future(self._drain(conn))
        self._backoff.pop(target.call, None)

    async def _beacon(self, conn: _Conn, settings: AprsSettings, now: datetime) -> None:
        t = conn.target
        if t.lat is None or t.lon is None:
            return
        due = conn.last_beacon is None or now - conn.last_beacon >= timedelta(
            minutes=settings.beacon_interval_min
        )
        if due and await self._write(
            conn, beacon_line(t.call, t.lat, t.lon, settings.beacon_comment)
        ):
            conn.last_beacon = now

    # -- main loop ---------------------------------------------------------------------

    async def step(self) -> None:
        now = self._now()
        desired, settings, own_calls = await self._load_state()

        for call in list(self._conns):
            conn = self._conns[call]
            if call not in desired or desired[call].station_id != conn.target.station_id:
                await self._close(call)
            elif conn.closed:
                await self._close(call)
                self._fail(call, now)
        for call, target in desired.items():
            if call not in self._conns:
                await self._login(target, settings, now)
        for call in list(self._backoff):
            if call not in desired:
                del self._backoff[call]

        for conn in list(self._conns.values()):
            await self._beacon(conn, settings, now)

        await self._gate_pending(desired, settings, own_calls, now)

        if (
            self._last_housekeeping is None
            or now - self._last_housekeeping >= HOUSEKEEPING_INTERVAL
        ):
            self._last_housekeeping = now
            await self.housekeeping(now)

    async def _gate_pending(self, desired, settings, own_calls, now) -> None:
        packets = await self._pending_packets()
        if not packets:
            return
        by_station = {c.target.station_id: c for c in self._conns.values()}
        wanted = {t.station_id for t in desired.values()}
        max_age = timedelta(seconds=settings.max_gate_age_s)
        orphans = {p.station_id for p in packets if p.station_id not in by_station}
        reasons = await self._skip_reasons_for(orphans) if orphans else {}
        for p in packets:
            if now - p.heard_at > max_age:
                await self._mark(p.pk, gate_skip="stale")
                continue
            conn = by_station.get(p.station_id)
            if conn is None or conn.closed:
                if p.station_id in wanted:
                    continue  # login pending / backing off: goes stale if it takes too long
                await self._mark(p.pk, gate_skip=reasons.get(p.station_id, "not_running"))
                continue
            line, skip = gate_line(p, conn.target.call, own_calls)
            if line is None:
                await self._mark(p.pk, gate_skip=skip)
            elif await self._write(conn, line):
                await self._mark(p.pk, gated_at=now)
            else:
                by_station.pop(p.station_id, None)

    @staticmethod
    @sync_to_async
    def _housekeeping_sync(cutoff: datetime) -> int:
        total = 0
        while True:
            ids = list(
                AprsPacket.objects.filter(heard_at__lt=cutoff).values_list("pk", flat=True)[
                    :HOUSEKEEPING_BATCH
                ]
            )
            if not ids:
                return total
            total += AprsPacket.objects.filter(pk__in=ids).delete()[0]

    async def housekeeping(self, now: datetime) -> int:
        settings = await sync_to_async(AprsSettings.get)()
        return await self._housekeeping_sync(now - timedelta(days=settings.retention_days))

    async def run_forever(self, poll_s: float = 1.0) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("aprs_igate step failed")
            await self._sleep(poll_s)

    async def shutdown(self) -> None:
        for call in list(self._conns):
            await self._close(call)
