"""SSTV transmit on the server (SSTV spec §5.4).

Preview → (guards at send time) → audio synthesis → ``sstv_tx_start`` to the agent, which
downloads the audio, keys, plays and unkeys on its own. The agent's ``sstv_tx_state``
frames end the transmission: ``done`` moves the preview into the gallery, anything else
keeps nothing to send again (the draft is purged by retention).

A transmission is ``sending`` from the moment its guards passed until the agent reports
the end or the server aborts it (lock loss, agent disconnect, operator stop). The server
never resends ``sstv_tx_start``: a reconnecting agent must not key again.
"""

import hashlib
import io
import logging
import uuid
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.control.models import StationModule
from station_agent.sstv.encoder import encode
from station_agent.sstv.modes import ISS_FREQUENCY_HZ, ISS_GUARD_HZ, MODES, duration_s

from . import control, render
from .ingest import image_frame
from .models import SstvImage, SstvTransmission

logger = logging.getLogger(__name__)

DRAFT_MAX_AGE = timedelta(hours=1)
TX_REQUEST_PREFIX = f"{control.REQUEST_PREFIX}tx-"
# Agent-side bound on the artefact (station_agent.sstv_tx.MAX_AUDIO_BYTES).
MAX_AUDIO_BYTES = 5 * 1024 * 1024
_AGENT_STATES = frozenset({"downloading", "keyed", "done", "aborted", "error"})
_END_STATES = frozenset({"done", "aborted", "error"})


class PreviewError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _caps(module):
    descriptor = module.capability_descriptor
    if not isinstance(descriptor, list):
        return {}
    return {
        c["name"]: c for c in descriptor if isinstance(c, dict) and isinstance(c.get("name"), str)
    }


def tx_module(station, slot=None, module=None):
    """The online module to transmit with: the given one, else the first that can tune TX."""
    rows = StationModule.objects.filter(station=station, online=True).order_by("slot", "module_id")
    if slot is not None or module is not None:
        if not isinstance(slot, str) or not isinstance(module, str):
            return None
        rows = rows.filter(slot=slot, module_id=module)
    for row in rows:
        if str(row.slot).isdigit() and "tx_frequency" in _caps(row):
            return row
    return None


def callsigns_for(station, user, *, cs_user, cs_station):
    calls = []
    if cs_user and user.username.strip():
        calls.append(user.username.strip())
    if cs_station and (station.callsign or "").strip():
        calls.append(station.callsign.strip())
    return calls


def create_preview(station, user, *, source, upload, mode_key, cs_user, cs_station, slot=None,
                   module=None, now=None):  # fmt: skip
    """Render the picture exactly as it will be sent and store it as the user's draft.

    Replaces the user's previous drafts for the station. Raises ``PreviewError`` (codes
    ``bad_mode``, ``bad_source``, ``no_file``, ``no_module``) or ``render.UploadError``."""
    mode = MODES.get(mode_key)
    if mode is None:
        raise PreviewError("bad_mode")
    target = tx_module(station, slot, module)
    if target is None:
        raise PreviewError("no_module")
    calls = callsigns_for(station, user, cs_user=cs_user, cs_station=cs_station)
    if source == "test":
        picture = render.test_picture(mode, calls, station.name, now or timezone.now())
    elif source == "upload":
        if upload is None:
            raise PreviewError("no_file")
        picture = render.overlay_callsigns(
            render.fit_to_mode(render.load_upload(upload), mode), calls
        )
    else:
        raise PreviewError("bad_source")
    buf = io.BytesIO()
    picture.save(buf, format="PNG")
    with transaction.atomic():
        SstvTransmission.objects.filter(
            station=station, user=user, state=SstvTransmission.State.DRAFT
        ).delete()
        tx = SstvTransmission(
            station=station,
            slot=int(target.slot),
            module=target.module_id,
            user=user,
            mode=mode.key,
            duration_s=duration_s(mode),
            callsigns=calls,
        )
        tx.preview.save(f"{uuid.uuid4().hex}.png", ContentFile(buf.getvalue()), save=False)
        tx.save()
    return tx


def _tx_frequency_mhz(module):
    state = module.last_state if isinstance(module.last_state, dict) else {}
    value = state.get("tx_frequency")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def _in_ranges(module, mhz):
    ranges = _caps(module).get("tx_frequency", {}).get("ranges")
    if not isinstance(ranges, list):
        return False
    for rng in ranges:
        try:
            if float(rng["min"]) <= mhz <= float(rng["max"]):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def frequency_guard(module):
    """``(code, hz)``: the module's current TX frequency checked against the ISS downlink
    and the module's TX ranges. Evaluated at send time, never at preview time."""
    if module is None:
        return "module_offline", None
    mhz = _tx_frequency_mhz(module)
    if mhz is None or not 0 < mhz < 1e6:
        return "no_frequency", None
    hz = round(mhz * 1_000_000)
    if abs(hz - ISS_FREQUENCY_HZ) <= ISS_GUARD_HZ:
        return "iss_downlink", hz
    if not _in_ranges(module, mhz):
        return "out_of_range", hz
    return None, hz


def _parse_id(value):
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def claim(station, user, transmission_id, *, confirm_no_callsign):
    """Run the send guards, in order, against the CURRENT state; on success the draft is
    ``sending`` (so a second send — other tab, double click — gets ``sstv_busy``).

    Returns ``(error_code, None)`` or ``(None, transmission)``."""
    if not control.is_holder(station, user):
        return "not_locked", None
    if not control.supported(station):
        return "unsupported", None
    tx_id = _parse_id(transmission_id)
    if tx_id is None:
        return "not_found", None
    with transaction.atomic():
        # Serialises concurrent sends of the station (no-op lock on SQLite).
        type(station).objects.select_for_update().filter(pk=station.pk).first()
        tx = SstvTransmission.objects.filter(pk=tx_id, station=station, user=user).first()
        if tx is None:
            return "not_found", None
        if SstvTransmission.objects.filter(
            station=station, state=SstvTransmission.State.SENDING
        ).exists():
            return "sstv_busy", None
        if tx.state != SstvTransmission.State.DRAFT:
            return "not_found", None
        if timezone.now() - tx.created_at > DRAFT_MAX_AGE:
            return "expired", None
        if not tx.callsigns and not confirm_no_callsign:
            return "no_callsign", None
        module = StationModule.objects.filter(
            station=station, slot=str(tx.slot), module_id=tx.module, online=True
        ).first()
        code, hz = frequency_guard(module)
        if code is not None:
            return code, None
        tx.state = SstvTransmission.State.SENDING
        tx.frequency_hz = hz
        tx.save(update_fields=["state", "frequency_hz", "updated_at"])
        control._audit(
            station, user, f"{user.username} started SSTV transmission ({tx.mode}, {hz} Hz)"
        )
    return None, tx


def synthesise(tx):
    """The transmission's audio (s16le mono 8 kHz). Slow-ish (≈1 s for the longest mode):
    callers run it off the event loop and off the DB thread."""
    mode = MODES[tx.mode]
    with tx.preview.open("rb") as f, Image.open(f) as img:
        picture = img.convert("RGB")
    if picture.size != (mode.width, mode.height):
        raise ValueError("preview does not match the mode")
    return encode(mode, render.to_rgb_bytes(picture))


def audio_path(tx):
    return reverse("sstv_api:tx_audio", args=[tx.pk])


def _tx_frame(tx, state, progress=0.0, detail=None):
    return control._frame(
        "sstv_tx_state",
        id=str(tx.pk),
        state=state,
        progress=progress,
        detail=control._clip(detail),
        mode=tx.mode,
        duration_s=tx.duration_s,
    )


def start_sending(station, tx, audio):
    """Store the audio and hand the transmission to the agent. ``None`` or an error code
    (the transmission was aborted meanwhile, or the audio is too large)."""
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        fail(station, tx.pk, "audio_too_large")
        return "internal_error"
    with transaction.atomic():
        row = (
            SstvTransmission.objects.select_for_update()
            .filter(pk=tx.pk, state=SstvTransmission.State.SENDING)
            .first()
        )
        if row is None:
            return "aborted"  # lock lost / agent gone while synthesising
        row.sha256 = hashlib.sha256(audio).hexdigest()
        row.size = len(audio)
        row.audio.save(f"{uuid.uuid4().hex}.raw", ContentFile(audio), save=False)
        row.save(update_fields=["sha256", "size", "audio", "updated_at"])
        # TX pauses RX (spec §5.2): the server pauses and resumes, the agent does not.
        control.pause_rx(station)
        control.send_to_agent(
            station.pk,
            control._frame(
                "sstv_tx_start",
                request_id=f"{TX_REQUEST_PREFIX}{row.pk.hex}",
                tx_id=row.pk.hex,
                slot=row.slot,
                module=row.module,
                url=audio_path(row),
                sha256=row.sha256,
                size=row.size,
                duration_s=row.duration_s,
            ),
        )
        control.broadcast(station.pk, _tx_frame(row, "sending"))
    return None


def _finish(station, tx, state, detail):
    """End a sending transmission (``aborted``/``error``): drop the audio, keep nothing to
    send again, resume a paused RX."""
    tx.state = state
    tx.save(update_fields=["state", "updated_at"])
    if tx.audio:
        audio = tx.audio
        name, storage = audio.name, audio.storage
        tx.audio = None
        tx.save(update_fields=["audio", "updated_at"])
        transaction.on_commit(lambda: _delete_blob(storage, name))
    control.broadcast(station.pk, _tx_frame(tx, state, detail=detail))
    control.resume_rx(station)


def _delete_blob(storage, name):
    try:
        storage.delete(name)
    except Exception:
        logger.exception("sstv: could not delete TX audio %s", name)


def fail(station, tx_pk, detail):
    with transaction.atomic():
        tx = (
            SstvTransmission.objects.select_for_update()
            .filter(pk=tx_pk, station=station, state=SstvTransmission.State.SENDING)
            .first()
        )
        if tx is not None:
            control._audit(station, tx.user, f"SSTV transmission failed ({control._clip(detail)})")
            _finish(station, tx, SstvTransmission.State.ERROR, detail)


def _done(station, tx):
    """On the air from key-up to key-down: the preview becomes the gallery entry."""
    with tx.preview.open("rb") as f:
        png = f.read()
    image = SstvImage(
        station=station,
        direction=SstvImage.Direction.TX,
        mode=tx.mode,
        frequency_hz=tx.frequency_hz,
        user=tx.user,
        callsign=tx.user.username if tx.user is not None else "",
        complete=True,
    )
    image.image.save(f"{uuid.uuid4().hex}.png", ContentFile(png), save=False)
    image.save()
    control._audit(station, tx.user, "SSTV transmission done")
    control.broadcast(station.pk, _tx_frame(tx, "done", 1.0))
    control.broadcast(station.pk, image_frame(image))
    tx.delete()  # preview + audio files go with the row (signals)
    control.resume_rx(station)


def _sending(station, tx_id):
    pk = _parse_id(tx_id) if isinstance(tx_id, str) and len(tx_id) == 32 else None
    if pk is None:
        return None
    return (
        SstvTransmission.objects.select_for_update()
        .select_related("user")
        .filter(pk=pk, station=station, state=SstvTransmission.State.SENDING)
        .first()
    )


def on_tx_state(station, msg):
    """Agent ``sstv_tx_state{tx_id, state, progress, detail}``."""
    state = msg.get("state")
    if state not in _AGENT_STATES:
        return
    progress = msg.get("progress")
    if (
        isinstance(progress, bool)
        or not isinstance(progress, (int, float))
        or progress != progress
    ):
        progress = 0.0
    progress = min(max(float(progress), 0.0), 1.0)
    detail = msg.get("detail") if isinstance(msg.get("detail"), str) else None
    with transaction.atomic():
        tx = _sending(station, msg.get("tx_id"))
        if tx is None:
            return  # unknown, or already ended by the server (abort)
        if state == "done":
            _done(station, tx)
        elif state in _END_STATES:
            control._audit(
                station, tx.user, f"SSTV transmission {state} ({control._clip(detail)})"
            )
            _finish(station, tx, state, detail)
        else:
            control.broadcast(station.pk, _tx_frame(tx, state, progress, detail))


def on_result(station, msg):
    """``result`` of our ``sstv_tx_start``: a refusal (``sstv_busy``, ``slot_busy``,
    ``updating``, ``unsupported``, ``bad_request``) ends the transmission."""
    rid = msg.get("request_id")
    if not isinstance(rid, str) or not rid.startswith(TX_REQUEST_PREFIX):
        return
    if msg.get("ok") is True:
        return
    error = msg.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    fail(station, _parse_id(rid[len(TX_REQUEST_PREFIX) :]), code or "refused")


def abort_tx(station, reason, *, notify_agent=True, unless_user=None):
    """Abort the station's transmission (lock loss, agent gone, operator stop). The agent
    gets ``sstv_tx_stop`` and unkeys; the download of an aborted transmission is refused
    from now on. ``unless_user``: keep a transmission of that user (preempting oneself)."""
    with transaction.atomic():
        rows = SstvTransmission.objects.select_for_update().filter(
            station=station, state=SstvTransmission.State.SENDING
        )
        if unless_user is not None:
            rows = rows.exclude(user=unless_user)
        aborted = False
        for tx in rows:
            if notify_agent:
                control.send_to_agent(station.pk, control._frame("sstv_tx_stop", tx_id=tx.pk.hex))
            control._audit(
                station, tx.user, f"SSTV transmission aborted ({control._clip(reason)})"
            )
            _finish(station, tx, SstvTransmission.State.ABORTED, reason)
            aborted = True
    return aborted


def sending_frame(station):
    """``sstv_tx_state`` of the transmission on the air (for a freshly loaded page)."""
    tx = SstvTransmission.objects.filter(
        station=station, state=SstvTransmission.State.SENDING
    ).first()
    return _tx_frame(tx, "sending") if tx is not None else None
