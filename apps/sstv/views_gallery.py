"""Station gallery: partial, picture/download, delete.

Pictures are always streamed through these permission-checked views, never via a
storage URL (S3 presigned URLs would work for anyone holding the link).

Delete rules (spec §6): ``sstv.use`` deletes the user's **own sent** pictures,
``sstv.manage`` deletes any picture of the station. The lock is not required —
deleting a gallery entry never touches the radio.
"""

from datetime import UTC

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils.text import slugify
from django.views import View

from apps.accounts.permissions import AreaPermissionMixin, can, require
from apps.stations.models import Station

from .models import SstvImage

PAGE_SIZE = 24
# Upper bound for ?page= — keeps the OFFSET (and int parsing) sane.
MAX_PAGE = 10_000


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


def _page(request) -> int:
    raw = request.GET.get("page", "1")
    if not raw.isdigit() or len(raw) > 6:
        return 1
    return min(max(1, int(raw)), MAX_PAGE)


class SstvGalleryView(AreaPermissionMixin, View):
    """Gallery partial, newest first. Page 1 renders the wrapper, later pages
    only their tiles plus the next "load more" button (HTMX swap)."""

    area = "sstv"
    verb = "view"

    def get(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        page = _page(request)
        start = (page - 1) * PAGE_SIZE
        rows = list(
            SstvImage.objects.filter(station=station)
            .select_related("user")
            .order_by("-created_at", "-pk")[start : start + PAGE_SIZE + 1]
        )
        has_more = len(rows) > PAGE_SIZE
        images = rows[:PAGE_SIZE]
        for img in images:
            img.station = station  # no extra query in templates/filename
            img.deletable = can_delete(request.user, img)
            img.frequency_mhz = img.frequency_hz / 1_000_000 if img.frequency_hz else None
        return TemplateResponse(
            request,
            "sstv/_gallery.html",
            {
                "station": station,
                "images": images,
                "page": page,
                "next_page": page + 1 if has_more else None,
                "first_page": page == 1,
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
