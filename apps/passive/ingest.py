import base64
import binascii
import logging
import time
from datetime import datetime, timedelta

from django.db import DataError, IntegrityError, transaction
from django.db.models import F

from apps.stations.models import Station

from .aprs.ax25 import Ax25Error, decode_ax25, is_digipeated, to_tnc2
from .aprs.parse import parse_info
from .coverage import record_coverage
from .models import AprsPacket, PassiveServiceRun

logger = logging.getLogger(__name__)

MAX_SKEW = timedelta(days=7)
_BAD_LOG_INTERVAL_S = 60.0
MAX_SEQ = 2**63 - 1  # BigIntegerField
MAX_FRAME_BYTES = 1024  # an APRS UI frame is far smaller; refuse oversized junk
_last_bad_log: dict[int, float] = {}


def server_heard_at(recv_now: datetime, ts: float, agent_heard_at: float) -> datetime:
    """Only agent-clock *differences* are trusted, never the absolute agent clock."""
    age = min(max(ts - agent_heard_at, 0.0), MAX_SKEW.total_seconds())
    return recv_now - timedelta(seconds=age)


def _log_bad(station_id: int, why: str) -> None:
    now = time.monotonic()
    if now - _last_bad_log.get(station_id, -_BAD_LOG_INTERVAL_S) >= _BAD_LOG_INTERVAL_S:
        _last_bad_log[station_id] = now
        logger.warning("station %s: undecodable AX.25 frame dropped (%s)", station_id, why)


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def ingest_output(station_id: int, msg: dict, *, recv_now: datetime) -> bool:
    """Store one ``decoder_output`` frame. True = ack it (stored, duplicate, ignored or
    undecodable); False = malformed epoch/seq, never acked."""
    epoch, seq = msg.get("epoch"), msg.get("seq")
    if not isinstance(epoch, str) or not epoch or len(epoch) > 32:
        return False
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= MAX_SEQ:
        return False
    if msg.get("kind") != "ax25":
        return True
    data = msg.get("data")
    if not isinstance(data, dict):
        _log_bad(station_id, "no data")
        return True
    raw = data.get("ax25", "")
    if not isinstance(raw, str) or len(raw) > MAX_FRAME_BYTES * 2:
        _log_bad(station_id, "oversized frame")
        return True
    try:
        frame = decode_ax25(base64.b64decode(data.get("ax25", ""), validate=True))
    except (binascii.Error, ValueError, TypeError, Ax25Error) as exc:
        _log_bad(station_id, str(exc))
        return True
    ts, agent_heard = _num(msg.get("ts")), _num(data.get("heard_at"))
    heard_at = (
        server_heard_at(recv_now, ts, agent_heard)
        if ts is not None and agent_heard is not None
        else recv_now
    )
    if b"\0" in frame.info:  # PostgreSQL text cannot hold NUL; such a frame is not valid APRS
        _log_bad(station_id, "NUL byte")
        return True
    info = parse_info(frame.info, frame.destination)
    try:
        with transaction.atomic():
            station = Station.objects.get(pk=station_id)
            run = PassiveServiceRun.open_for(station_id)
            packet = AprsPacket.objects.create(
                station=station,
                run=run,
                epoch=epoch,
                seq=seq,
                heard_at=heard_at,
                received_at=recv_now,
                raw_tnc2=to_tnc2(frame),
                source=frame.source,
                destination=frame.destination,
                path=list(frame.path),
                digipeated=is_digipeated(frame.path),
                packet_type=info.packet_type,
                latitude=None if info.latitude is None else round(info.latitude, 6),
                longitude=None if info.longitude is None else round(info.longitude, 6),
                symbol=info.symbol,
                comment=info.comment[:256],
            )
            if run is not None:
                PassiveServiceRun.objects.filter(pk=run.pk).update(
                    last_packet_at=recv_now, packet_count=F("packet_count") + 1
                )
            record_coverage(station, packet)
    except DataError as exc:  # content the DB refuses: drop deterministically, never retry
        _log_bad(station_id, f"rejected by DB: {exc}")
    except IntegrityError:
        if AprsPacket.objects.filter(station_id=station_id, epoch=epoch, seq=seq).exists():
            return True  # duplicate (station, epoch, seq)
        raise  # anything else (FK, coverage race): unacked, the agent resends
    except Station.DoesNotExist:
        return False
    return True
