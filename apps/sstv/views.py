"""SSTV views of the station control page (TX preview)."""

from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.permissions import AreaPermissionMixin
from apps.stations.models import Station

from . import control, render, tx
from .models import SstvTransmission

_PREVIEW_ERRORS = {
    "bad_mode": lambda: _("Unknown SSTV mode."),
    "bad_source": lambda: _("Choose a photo or the test picture."),
    "no_file": lambda: _("Choose a photo to send."),
    "no_module": lambda: _("No module of this station can transmit."),
}


def _flag(request, name):
    return request.POST.get(name) in ("1", "true", "on")


class PreviewView(AreaPermissionMixin, View):
    """``POST``: render the picture as it will be sent and store it as the draft.
    Lock holder with ``sstv.use`` only."""

    area = "sstv"
    verb = "use"

    def post(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        if not control.is_holder(station, request.user):
            return JsonResponse(
                {"error": "not_locked", "message": _("You do not hold the lock.")}, status=403
            )
        try:
            draft = tx.create_preview(
                station,
                request.user,
                source=request.POST.get("source"),
                upload=request.FILES.get("file"),
                mode_key=request.POST.get("mode"),
                cs_user=_flag(request, "cs_user"),
                cs_station=_flag(request, "cs_station"),
                slot=request.POST.get("slot") or None,
                module=request.POST.get("module") or None,
            )
        except render.UploadError as exc:
            return JsonResponse({"error": exc.code, "message": str(exc)}, status=400)
        except tx.PreviewError as exc:
            return JsonResponse(
                {"error": exc.code, "message": str(_PREVIEW_ERRORS[exc.code]())}, status=400
            )
        body = {
            "id": str(draft.pk),
            "preview_url": draft_url(draft),
            "duration_s": round(draft.duration_s, 1),
            "mode": draft.mode,
            "slot": draft.slot,
            "module": draft.module,
            "warnings": [] if draft.callsigns else ["no_callsign"],
        }
        return JsonResponse(body)


def draft_url(draft):
    return reverse("sstv:preview_image", args=[draft.station_id, draft.pk])


class PreviewImageView(AreaPermissionMixin, View):
    """``GET``: the stored preview PNG of one of the caller's own transmissions."""

    area = "sstv"
    verb = "use"

    def get(self, request, pk, tx_id):
        draft = SstvTransmission.objects.filter(pk=tx_id, station_id=pk, user=request.user).first()
        if draft is None or not draft.preview:
            raise Http404
        response = FileResponse(draft.preview.open("rb"), content_type="image/png")
        response["Cache-Control"] = "private, no-store"
        return response
