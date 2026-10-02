"""Read-only viewsets for the user/automation API (v1)."""

from rest_framework.authentication import SessionAuthentication
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.api.authentication import PersonalAccessTokenAuthentication
from apps.api.permissions import TopologyScopedPermission
from apps.api.read_filters import StationFilter
from apps.api.read_serializers import RegionSerializer, StationSerializer, StationTagSerializer
from apps.stations.models import StationTag
from apps.stations.scoping import accessible_regions, accessible_stations


class ScopedReadOnlyViewSet(ReadOnlyModelViewSet):
    """Base: bearer/session auth, membership gate, token throttle."""

    authentication_classes = [PersonalAccessTokenAuthentication, SessionAuthentication]
    permission_classes = [TopologyScopedPermission]
    throttle_scope = "api-token"


class StationViewSet(ScopedReadOnlyViewSet):
    serializer_class = StationSerializer
    filterset_class = StationFilter
    search_fields = ["name", "callsign", "location_name"]
    ordering_fields = ["name", "last_seen", "created_at"]

    def get_queryset(self):
        return accessible_stations(self.request.user).order_by("name")


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
