import logging

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice
from apps.api.serializers import HealthSerializer, HeartbeatSerializer
from apps.stations.models import Station, StationInventory

logger = logging.getLogger(__name__)


class HealthCheckView(APIView):
    """Public health-check endpoint."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        serializer = HealthSerializer({"status": "ok"})
        return Response(serializer.data)


class HeartbeatView(APIView):
    """Receives periodic heartbeat data from station agents."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]
    throttle_scope = "heartbeat"

    def post(self, request):
        serializer = HeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        # request.auth is a DeviceKey instance; resolve the linked station.
        station = request.auth.station
        station_label = str(station)

        logger.info(
            "Heartbeat from %s: %s",
            station_label,
            data.get("hostname"),
        )

        old_status = station.status
        old_os = station.current_os_version
        old_ip = str(station.last_ip_address or "")

        station.current_os_version = data.get("os_version", "")
        station.current_agent_version = data.get("agent_version", "")
        station.last_ip_address = str(data.get("ip_address", ""))
        station.last_seen = timezone.now()
        station.status = "online"
        station.save(
            update_fields=[
                "current_os_version",
                "current_agent_version",
                "last_ip_address",
                "last_seen",
                "status",
            ],
        )

        # Audit log for notable changes
        from apps.stations.models import StationAuditLog

        if old_status != "online":
            StationAuditLog.log(
                station=station,
                event_type=StationAuditLog.EventType.STATUS_CHANGE,
                message=f"Station came online (was {old_status}).",
                changes={"status": {"old": old_status, "new": "online"}},
            )
        if old_os and old_os != data.get("os_version", ""):
            StationAuditLog.log(
                station=station,
                event_type=StationAuditLog.EventType.HEARTBEAT,
                message=f"OS version changed: {old_os} → {data['os_version']}",
                changes={"os_version": {"old": old_os, "new": data["os_version"]}},
            )
        new_ip = str(data.get("ip_address", ""))
        if old_ip and old_ip != new_ip:
            StationAuditLog.log(
                station=station,
                event_type=StationAuditLog.EventType.HEARTBEAT,
                message=f"IP address changed: {old_ip} → {new_ip}",
                changes={"ip_address": {"old": old_ip, "new": new_ip}},
            )

        # Persist inventory data if provided.
        inventory_data = data.get("inventory")
        if inventory_data:
            StationInventory.objects.update_or_create(
                station=station,
                defaults={"data": inventory_data},
            )

        # Broadcast updated status to WebSocket clients.
        try:
            from apps.stations.consumers import broadcast_station_status

            broadcast_station_status(station)
        except Exception:
            logger.exception("Failed to broadcast station status via WebSocket.")

        return Response(
            {"status": "ok"},
            status=status.HTTP_200_OK,
        )


class StationInventoryView(APIView):
    """Returns the hardware inventory for a station.

    GET /api/v1/stations/<station_id>/inventory/
    """

    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, station_id):
        if request.user.role not in ("admin", "operator"):
            return Response(
                {"detail": "Admin or operator role required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        station = get_object_or_404(Station, pk=station_id)
        try:
            inventory = station.inventory
        except StationInventory.DoesNotExist:
            return Response(
                {"detail": "No inventory data available for this station."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "station_id": station.pk,
                "data": inventory.data,
                "updated_at": inventory.updated_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )
