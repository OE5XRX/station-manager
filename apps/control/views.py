from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View
from django.views.generic import DetailView

from apps.accounts.permissions import (
    AreaPermissionMixin,
    can_override_station,
    level_label,
    required_level,
)
from apps.module_firmware import drain
from apps.module_firmware.update_status import bulk_update_status
from apps.repeaters.selection import repeaters_for_station, station_bands
from apps.repeaters.views import ATTRIBUTION
from apps.stations.models import Station

from . import serializers


class StationControlView(AreaPermissionMixin, DetailView):
    """Control panel. ``control.view`` renders it read-only (RX audio still available);
    lock/PTT/mic controls render only with ``control.use`` (template ``can.control.use``)."""

    area = "control"
    verb = "view"
    model = Station
    template_name = "control/panel.html"
    context_object_name = "station"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        station = self.object
        # The audio-router is a virtual control-plane module (stream enumeration +
        # tx_route) owned by the dedicated audio panel — not a generic module card.
        # Rendering its `streams` (a list) in the generic widget shows
        # "[object Object],[object Object]". tx_route still flows over the control-WS
        # command path, so hiding the card changes nothing functional.
        ctx["modules"] = [
            m
            for m in station.modules.select_related("tracked_module__module_type").all()
            if m.type != "audio_router"
        ]
        # Firmware update pill per tracked module (one bulk pass). Resolved via
        # each module's open assignment — like the HTMX poll — since a tracked
        # module may already be assigned to another station.
        tracked = [m.tracked_module for m in ctx["modules"] if m.tracked_module is not None]
        statuses = bulk_update_status(tracked)
        for m in ctx["modules"]:
            if m.tracked_module is not None:
                m.update_status = statuses.get(m.tracked_module.pk)
        ctx["initial_inventory"] = serializers.snapshot(station)
        # Cosmetic (Override button); the consumer re-checks on every preempt.
        ctx["can_admin"] = can_override_station(self.request.user, station)
        # Read-only hint: which level unlocks operating (from the registry, not hardcoded).
        ctx["control_use_label"] = level_label(required_level("control", "use"))
        ctx["ptt_default_key"] = " "
        # Station drain banner first paint; live updates come via lock frames.
        ctx["drain"] = drain.drain_status(station)
        return ctx


# Search terms beyond this are noise (callsign/site are <= 128 chars).
_MAX_QUERY = 64


class StationRepeatersView(AreaPermissionMixin, View):
    """Repeater panel data: usable repeaters for this station's online bands (spec §5.2).

    ``control.view`` like the panel itself; applying a repeater is gated in the consumer.
    """

    area = "control"
    verb = "view"

    def get(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        q = request.GET.get("q", "").strip()[:_MAX_QUERY]
        bands = {
            band: [{"slot": ref.slot, "module": ref.module} for ref in refs]
            for band, refs in station_bands(station).items()
        }
        rows = [
            {
                "id": row.repeater.pk,
                "callsign": row.repeater.callsign,
                "site_name": row.repeater.site_name,
                "output_frequency": str(row.repeater.output_frequency),
                "input_frequency": str(row.repeater.input_frequency),
                "input_tone": row.repeater.input_tone,
                "output_tone": row.repeater.output_tone,
                "bandwidth": row.repeater.bandwidth,
                "distance_km": None if row.distance_km is None else round(row.distance_km, 1),
                "bearing": row.bearing,
                "rx_band": row.rx_band,
                "tx_band": row.tx_band,
                "cross_band": row.cross_band,
            }
            for row in repeaters_for_station(station, q=q)
        ]
        return JsonResponse(
            {
                "bands": bands,
                "station_has_coords": station.latitude is not None
                and station.longitude is not None,
                "attribution": ATTRIBUTION,
                "repeaters": rows,
            }
        )
