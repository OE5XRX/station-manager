"""StationSetRegionView: admin-only POST endpoint to set Station.region.

The Station.region change is detected by apps/stations/signals.py
(_on_station_pre_save + _on_station_save), which emits
STATION_REGION_CHANGED on StationAuditLog. No view-level emission
needed.
"""

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Region, Station


class StationSetRegionView(AdminRequiredMixin, View):
    def post(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        region_pk = request.POST.get("region", "").strip()
        if region_pk:
            # Validate the POSTed value is integer-shaped before
            # handing it to the ORM. Without this guard a malformed
            # value (e.g. `region=abc`) raises ValueError during PK
            # coercion and surfaces as a 500.
            try:
                region_pk_int = int(region_pk)
            except ValueError as exc:
                raise Http404("Invalid region id") from exc
            new_region = get_object_or_404(Region, pk=region_pk_int)
            new_region_id = new_region.pk
        else:
            new_region = None
            new_region_id = None
        # Short-circuit on no-op: avoids a wasted UPDATE and an
        # `updated_at` bump that doesn't reflect any actual change.
        if station.region_id == new_region_id:
            return JsonResponse({"success": True})
        station.region = new_region
        # Include `updated_at` so the auto_now bump fires under
        # update_fields (matches the heartbeat pattern on Station).
        station.save(update_fields=["region", "updated_at"])
        return JsonResponse({"success": True})
