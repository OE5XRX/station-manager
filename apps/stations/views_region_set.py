"""StationSetRegionView: admin-only POST endpoint to set Station.region.

The Station.region change is detected by apps/stations/signals.py
(_on_station_pre_save + _on_station_save), which emits
STATION_REGION_CHANGED on StationAuditLog. No view-level emission
needed.
"""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Region, Station


class StationSetRegionView(AdminRequiredMixin, View):
    def post(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        region_pk = request.POST.get("region", "").strip()
        if region_pk:
            station.region = get_object_or_404(Region, pk=region_pk)
        else:
            station.region = None
        station.save(update_fields=["region"])
        return JsonResponse({"success": True})
