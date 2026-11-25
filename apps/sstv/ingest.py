"""Agent ``decoder_output`` frames of the ``sstv`` decoder (SSTV spec §5.3).

``sstv_start`` and ``sstv_line`` only feed the live view of the control page and are never
stored; ``sstv_image`` (reliable, spooled by the agent) is the authoritative picture and
becomes a gallery entry, deduplicated on the agent's ``picture_id``.
"""

import base64
import binascii
import io
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.urls import NoReverseMatch, reverse
from PIL import Image

from apps.control.models import StationModule
from apps.passive.ingest import MAX_SEQ, server_heard_at
from apps.stations.models import Station
from station_agent.sstv.modes import MODES

from . import control
from .models import ActiveSstvRx, SstvImage

logger = logging.getLogger(__name__)

_PICTURE_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}")
# The agent keeps the PNG's base64 under 900 KB (daphne's default websocket message limit
# is 1 MiB for the whole frame); anything larger is not from our agent.
MAX_PNG_B64 = 1_000_000
# A row of the widest mode (640 px RGB) is 2560 base64 characters.
MAX_LINE_B64 = 4096
MAX_ROW = 1024
# Pictures heard a little before the session row was written still belong to it (agent
# and server clocks are compared only via the ts difference, see server_heard_at).
_ATTRIBUTION_SLACK = timedelta(seconds=5)
_BAD_LOG_INTERVAL_S = 60.0
_last_bad_log: dict[int, float] = {}


def _log_bad(station_id, why):
    now = time.monotonic()
    if now - _last_bad_log.get(station_id, -_BAD_LOG_INTERVAL_S) >= _BAD_LOG_INTERVAL_S:
        _last_bad_log[station_id] = now
        logger.warning("station %s: SSTV frame dropped (%s)", station_id, why)


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _picture_id(data):
    pid = data.get("picture_id")
    return pid if isinstance(pid, str) and _PICTURE_ID_RE.fullmatch(pid) else None


def line_frame(msg):
    """Browser ``sstv_line`` for an agent ``sstv_line`` output, ``None`` if malformed.
    Pure (no DB): lines arrive several times a second."""
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    pid, row, rgb = _picture_id(data), data.get("row"), data.get("rgb")
    if pid is None or type(row) is not int or not 0 <= row < MAX_ROW:
        return None
    if not isinstance(rgb, str) or not rgb or len(rgb) > MAX_LINE_B64:
        return None
    return control._frame("sstv_line", picture_id=pid, row=row, rgb=rgb)


def _start_frame(data):
    pid, mode = _picture_id(data), MODES.get(data.get("mode"))
    if pid is None or mode is None:
        return None
    return control._frame(
        "sstv_start", picture_id=pid, mode=mode.key, width=mode.width, height=mode.height
    )


def image_url(image):
    """URL of a gallery picture (the gallery's image view), ``None`` if not routed."""
    try:
        return reverse("sstv:image", args=[image.station_id, image.pk])
    except NoReverseMatch:
        return None


def image_frame(image):
    return control._frame(
        "sstv_image",
        id=str(image.pk),
        url=image_url(image),
        mode=image.mode,
        direction=image.direction,
        complete=image.complete,
        created_at=image.created_at.isoformat(),
    )


def _frequency_hz(station, slot, cap):
    """``cap`` (MHz) from the slot's online module state, in Hz."""
    for module in StationModule.objects.filter(
        station=station, slot=str(slot), online=True
    ).order_by("module_id"):
        state = module.last_state if isinstance(module.last_state, dict) else {}
        value = _num(state.get(cap))
        if value is not None and 0 < value < 1e6:
            return round(value * 1_000_000)
    return None


def _valid_png(png, mode):
    try:
        with Image.open(io.BytesIO(png)) as img:
            if img.format != "PNG" or img.size != (mode.width, mode.height):
                return False
            img.load()
    except Exception:
        return False
    return True


def _store_image(station_id, msg, data, *, recv_now):
    pid, mode = _picture_id(data), MODES.get(data.get("mode"))
    png_b64, complete = data.get("png"), data.get("complete")
    slot = msg.get("slot")
    if pid is None or mode is None or not isinstance(complete, bool):
        _log_bad(station_id, "malformed sstv_image")
        return
    if type(slot) is not int or not isinstance(png_b64, str) or len(png_b64) > MAX_PNG_B64:
        _log_bad(station_id, "malformed sstv_image")
        return
    if SstvImage.objects.filter(station_id=station_id, picture_id=pid).exists():
        return  # resent after a reconnect: already in the gallery
    try:
        png = base64.b64decode(png_b64, validate=True)
    except (binascii.Error, ValueError):
        _log_bad(station_id, "sstv_image png not base64")
        return
    if not _valid_png(png, mode):
        _log_bad(station_id, "sstv_image png invalid")
        return
    ts, agent_heard = _num(msg.get("ts")), _num(data.get("heard_at"))
    heard_at = (
        server_heard_at(recv_now, ts, agent_heard)
        if ts is not None and agent_heard is not None
        else recv_now
    )
    stored = None  # the uploaded PNG, removed again if the row is not committed
    try:
        with transaction.atomic():
            station = Station.objects.get(pk=station_id)
            session = ActiveSstvRx.objects.filter(station=station, slot=slot).first()
            user = (
                session.user
                if session is not None and heard_at >= session.started_at - _ATTRIBUTION_SLACK
                else None
            )
            image = SstvImage(
                station=station,
                direction=SstvImage.Direction.RX,
                mode=mode.key,
                frequency_hz=_frequency_hz(station, slot, "rx_frequency"),
                user=user,
                complete=complete,
                picture_id=pid,
            )
            image.image.save(f"{uuid.uuid4().hex}.png", ContentFile(png), save=False)
            stored = image.image
            image.save()
            # The gallery shows when the picture was heard, not when it arrived.
            SstvImage.objects.filter(pk=image.pk).update(created_at=heard_at)
            image.created_at = heard_at
            control.broadcast(station.pk, image_frame(image))
    except Exception as exc:
        if stored is not None:
            _delete_quietly(stored.storage, stored.name)
        if isinstance(exc, IntegrityError) and (
            SstvImage.objects.filter(station_id=station_id, picture_id=pid).exists()
        ):
            return  # concurrent duplicate
        raise


def _delete_quietly(storage, name):
    try:
        storage.delete(name)
    except Exception:
        logger.exception("sstv: deleting the orphaned picture %s failed", name)


def ingest_output(station_id: int, msg: dict, *, recv_now: datetime) -> bool:
    """Handle one reliable ``sstv`` ``decoder_output``. True = ack it (stored, duplicate,
    broadcast or undecodable); False = malformed epoch/seq, never acked."""
    epoch, seq = msg.get("epoch"), msg.get("seq")
    if not isinstance(epoch, str) or not epoch or len(epoch) > 32:
        return False
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= MAX_SEQ:
        return False
    data = msg.get("data")
    if not isinstance(data, dict):
        _log_bad(station_id, "no data")
        return True
    kind = msg.get("kind")
    if kind == "sstv_start":
        frame = _start_frame(data)
        if frame is not None:
            control.broadcast(station_id, frame)
    elif kind == "sstv_image":
        try:
            _store_image(station_id, msg, data, recv_now=recv_now)
        except Station.DoesNotExist:
            return False
    return True
