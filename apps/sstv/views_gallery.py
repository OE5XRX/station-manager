"""Station gallery: partial, picture/download, delete.

Pictures are always streamed through these permission-checked views, never via a
storage URL (S3 presigned URLs would work for anyone holding the link).

Delete rules (spec §6): ``sstv.use`` deletes the user's **own sent** pictures,
``sstv.manage`` deletes any picture of the station. The lock is not required —
deleting a gallery entry never touches the radio.
"""

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from django.core.exceptions import BadRequest, PermissionDenied
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils.text import slugify
from django.views import View

from apps.accounts.permissions import AreaPermissionMixin, can, require
from apps.stations.models import Station

from .models import SstvImage

PAGE_SIZE = 24
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def can_delete(user, image) -> bool:
    if can(user, "sstv", "manage"):
        return True
    return (
        image.direction == SstvImage.Direction.TX
        and image.user_id is not None
        and image.user_id == user.pk
        and can(user, "sstv", "use")
    )


def download_filename(image) -> str:
    """``<station-slug>-<YYYYMMDD-HHMMSS>Z-<mode>-<rx|tx>.png`` (UTC, header-safe)."""
    station = image.station
    slug = slugify(station.name) or f"station-{station.pk}"
    stamp = image.created_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{stamp}Z-{slugify(image.mode)}-{image.direction}.png"


def _cursor_ts(dt) -> int:
    """Exact integer microseconds since the epoch (no float rounding)."""
    return (dt - _EPOCH) // timedelta(microseconds=1)


def _parse_cursor(request):
    """``(created_at, pk)`` of the last tile already shown, or ``None`` (first page).

    Keyset, not OFFSET: deleting a tile in place or a newly received picture
    never shifts the next page (no skipped or duplicated tiles).
    """
    raw_ts, raw_id = request.GET.get("ts"), request.GET.get("id")
    if raw_ts is None and raw_id is None:
        return None
    try:
        if not (raw_ts.isascii() and raw_ts.isdigit() and len(raw_ts) <= 18):
            raise ValueError(raw_ts)
        return _EPOCH + timedelta(microseconds=int(raw_ts)), uuid.UUID(raw_id)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise BadRequest("bad gallery cursor") from exc


class SstvGalleryView(AreaPermissionMixin, View):
    """Gallery partial, newest first. The first page renders the wrapper, later
    pages (``?ts=&id=`` cursor) only their tiles plus the next "load more" button
    (HTMX swap)."""

    area = "sstv"
    verb = "view"

    def get(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        cursor = _parse_cursor(request)
        qs = SstvImage.objects.filter(station=station)
        if cursor is not None:
            ts, last_pk = cursor
            qs = qs.filter(Q(created_at__lt=ts) | Q(created_at=ts, pk__lt=last_pk))
        rows = list(qs.select_related("user").order_by("-created_at", "-pk")[: PAGE_SIZE + 1])
        has_more = len(rows) > PAGE_SIZE
        images = rows[:PAGE_SIZE]
        for img in images:
            img.station = station  # no extra query in templates/filename
            img.deletable = can_delete(request.user, img)
            img.frequency_mhz = img.frequency_hz / 1_000_000 if img.frequency_hz else None
        next_query = None
        if has_more:
            last = images[-1]
            next_query = urlencode({"ts": _cursor_ts(last.created_at), "id": str(last.pk)})
        return TemplateResponse(
            request,
            "sstv/_gallery.html",
            {
                "station": station,
                "images": images,
                "next_query": next_query,
                "first_page": cursor is None,
            },
        )


class _PictureView(AreaPermissionMixin, View):
    area = "sstv"
    verb = "view"
    as_attachment = False

    def get(self, request, pk, image_id):
        image = get_object_or_404(
            SstvImage.objects.select_related("station"), pk=image_id, station_id=pk
        )
        try:
            fh = image.image.open("rb")
        except (FileNotFoundError, ValueError) as exc:
            raise Http404("picture file missing") from exc
        resp = FileResponse(
            fh,
            content_type="image/png",
            as_attachment=self.as_attachment,
            filename=download_filename(image),
        )
        # Pictures never change; only the logged-in browser may keep them.
        resp["Cache-Control"] = "private, max-age=3600"
        return resp


class SstvPictureView(_PictureView):
    """Inline picture for the gallery tiles."""


class SstvDownloadView(_PictureView):
    as_attachment = True


class SstvDeleteView(AreaPermissionMixin, View):
    """POST only. ``use`` is the floor (applicants get 403 before any lookup);
    anything but the user's own sent picture additionally needs ``manage``."""

    area = "sstv"
    verb = "use"

    def post(self, request, pk, image_id):
        image = get_object_or_404(SstvImage, pk=image_id, station_id=pk)
        if not can_delete(request.user, image):
            # Logs the denial like every other area check, then 403.
            require(request.user, "sstv", "manage", request=request)
            raise PermissionDenied  # pragma: no cover — require() raised
        image.delete()
        if request.headers.get("HX-Request"):
            # Empty body: the tile swaps itself out (hx-swap="outerHTML").
            return HttpResponse("")
        return redirect("control:station_control", pk=pk)
