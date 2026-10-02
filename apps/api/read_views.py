"""Read-only viewsets for the user/automation API (v1)."""

from django.db.models import Q
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.api.authentication import PersonalAccessTokenAuthentication
from apps.api.permissions import TopologyScopedPermission
from apps.api.read_filters import StationFilter
from apps.api.read_serializers import (
    RegionAssignmentSerializer,
    RegionSerializer,
    StationAssignmentSerializer,
    StationInventorySerializer,
    StationLogEntrySerializer,
    StationModuleSerializer,
    StationPhotoSerializer,
    StationSerializer,
    StationTagSerializer,
    StationTelemetrySerializer,
)
from apps.stations.models import RegionAssignment, StationAssignment, StationTag
from apps.stations.scoping import accessible_regions, accessible_stations


class ScopedReadOnlyViewSet(ReadOnlyModelViewSet):
    """Base: bearer/session auth, membership gate, token throttle."""

    authentication_classes = [PersonalAccessTokenAuthentication, SessionAuthentication]
    permission_classes = [TopologyScopedPermission]
    throttle_scope = "api-token"

    def _child_list(self, request, manager_or_qs, serializer_cls):
        """Paginate and serialize a related manager or queryset."""
        qs = manager_or_qs.all() if hasattr(manager_or_qs, "all") else manager_or_qs
        page = self.paginate_queryset(qs)
        ser = serializer_cls(page, many=True, context={"request": request})
        return self.get_paginated_response(ser.data)


class StationViewSet(ScopedReadOnlyViewSet):
    serializer_class = StationSerializer
    filterset_class = StationFilter
    search_fields = ["name", "callsign", "location_name"]
    ordering_fields = ["name", "last_seen", "created_at"]

    def get_queryset(self):
        return accessible_stations(self.request.user).order_by("name")

    @action(detail=True, url_path="telemetry")
    def telemetry(self, request, pk=None):
        from apps.stations.models import StationTelemetry

        station = self.get_object()
        obj = StationTelemetry.objects.filter(station=station).first()
        if obj is None:
            raise NotFound()
        return Response(StationTelemetrySerializer(obj, context={"request": request}).data)

    @action(detail=True, url_path="inventory")
    def inventory(self, request, pk=None):
        from apps.stations.models import StationInventory

        station = self.get_object()
        obj = StationInventory.objects.filter(station=station).first()
        if obj is None:
            raise NotFound()
        return Response(StationInventorySerializer(obj, context={"request": request}).data)

    @action(detail=True, url_path="log-entries")
    def log_entries(self, request, pk=None):
        station = self.get_object()
        return self._child_list(
            request,
            station.log_entries.order_by("-created_at"),
            StationLogEntrySerializer,
        )

    @action(detail=True, url_path="photos")
    def photos(self, request, pk=None):
        station = self.get_object()
        return self._child_list(
            request,
            station.photos.order_by("-uploaded_at"),
            StationPhotoSerializer,
        )

    @action(detail=True, url_path="modules")
    def modules(self, request, pk=None):
        station = self.get_object()
        return self._child_list(
            request,
            station.modules.order_by("slot"),
            StationModuleSerializer,
        )


class RegionViewSet(ScopedReadOnlyViewSet):
    serializer_class = RegionSerializer
    search_fields = ["name", "slug"]
    ordering_fields = ["name"]

    def get_queryset(self):
        return accessible_regions(self.request.user).order_by("name")


class StationTagViewSet(ScopedReadOnlyViewSet):
    serializer_class = StationTagSerializer
    search_fields = ["name", "slug"]
    ordering_fields = ["name"]

    def get_queryset(self):
        # Global taxonomy; any ≥member may read (applicants blocked by perm).
        return StationTag.objects.all().order_by("name")


class StationAssignmentViewSet(ScopedReadOnlyViewSet):
    serializer_class = StationAssignmentSerializer
    filterset_fields = ["station", "user", "role"]
    ordering_fields = ["assigned_at"]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return StationAssignment.objects.all().order_by("-assigned_at")
        return (
            StationAssignment.objects.filter(
                Q(station__in=accessible_stations(user)) | Q(user=user)
            )
            .distinct()
            .order_by("-assigned_at")
        )


class RegionAssignmentViewSet(ScopedReadOnlyViewSet):
    serializer_class = RegionAssignmentSerializer
    filterset_fields = ["region", "user", "role"]
    ordering_fields = ["assigned_at"]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return RegionAssignment.objects.all().order_by("-assigned_at")
        return (
            RegionAssignment.objects.filter(Q(region__in=accessible_regions(user)) | Q(user=user))
            .distinct()
            .order_by("-assigned_at")
        )
