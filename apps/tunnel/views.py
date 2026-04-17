from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.views import View

from apps.stations.models import Station

from .models import TerminalSession


class TerminalStatusView(LoginRequiredMixin, View):
    """Check terminal availability for a station (used by HTMX before opening WebSocket)."""

    def get(self, request, station_id):
        try:
            station = Station.objects.get(pk=station_id)
        except Station.DoesNotExist:
            return JsonResponse({"error": "Station not found"}, status=404)

        is_online = station.status == Station.Status.ONLINE
        active_sessions = TerminalSession.objects.filter(
            station=station,
            status__in=("connecting", "active"),
        ).count()
        can_connect = (
            is_online and active_sessions < 2 and request.user.role in ("admin", "operator")
        )

        return JsonResponse(
            {
                "station_id": station.id,
                "station_name": str(station),
                "is_online": is_online,
                "active_sessions": active_sessions,
                "can_connect": can_connect,
            }
        )
