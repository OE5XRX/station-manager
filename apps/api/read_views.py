"""Read-only viewsets for the user/automation API (v1)."""

from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.accounts.permissions import AreaPermission, Level, can, level_of
from apps.accounts.visibility import audience_for, directory_queryset
from apps.api.authentication import PersonalAccessTokenAuthentication
from apps.api.read_filters import (
    AlertFilter,
    DeploymentFilter,
    DeploymentResultFilter,
    ImageReleaseFilter,
    ProvisioningJobFilter,
    StationFilter,
)
from apps.api.read_serializers import (
    AlertRuleSerializer,
    AlertSerializer,
    DeploymentResultSerializer,
    DeploymentSerializer,
    ImageImportJobSerializer,
    ImageReleaseSerializer,
    ProvisioningJobSerializer,
    RegionAssignmentSerializer,
    RegionSerializer,
    RolloutSequenceEntrySerializer,
    RolloutSequenceSerializer,
    StationAssignmentSerializer,
    StationInventorySerializer,
    StationLogEntrySerializer,
    StationModuleSerializer,
    StationPhotoSerializer,
    StationSerializer,
    StationTagSerializer,
    StationTelemetrySerializer,
    UserSerializer,
)
from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageImportJob, ImageRelease
from apps.monitoring.models import Alert
from apps.provisioning.models import ProvisioningJob
from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationTag,
)


class AreaReadOnlyViewSet(ReadOnlyModelViewSet):
    """Base: bearer/session auth, area permission, token throttle.

    Concrete viewsets declare ``area``; safe methods need ``<area>.view``,
    everything else ``<area>.manage`` unless ``verb``/``verbs`` (or an
    ``@action(..., verb=...)``) say otherwise. No topology scoping: every
    queryset returns everything the area shows, exactly like the UI.
    """

    authentication_classes = [PersonalAccessTokenAuthentication, SessionAuthentication]
    permission_classes = [AreaPermission]
    throttle_scope = "api-token"
    area: str | None = None
    # ``verb`` must exist on the class so ``@action(..., verb=...)`` initkwargs
    # pass DRF's ``hasattr(cls, key)`` check in ``as_view``.
    verb: str | None = None
    verbs: dict[str, str] | None = None

    def _child_list(self, request, manager_or_qs, serializer_cls):
        """Paginate and serialize a related manager or queryset."""
        qs = manager_or_qs.all() if hasattr(manager_or_qs, "all") else manager_or_qs
        page = self.paginate_queryset(qs)
        ser = serializer_cls(page, many=True, context={"request": request})
        return self.get_paginated_response(ser.data)


class StationViewSet(AreaReadOnlyViewSet):
    area = "stations"
    serializer_class = StationSerializer
    filterset_class = StationFilter
    search_fields = ["name", "callsign", "location_name"]
    ordering_fields = ["name", "last_seen", "created_at"]

    def get_queryset(self):
        return Station.objects.all().order_by("name")

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


class RegionViewSet(AreaReadOnlyViewSet):
    area = "topology"
    serializer_class = RegionSerializer
    search_fields = ["name", "slug"]
    ordering_fields = ["name"]

    def get_queryset(self):
        return Region.objects.all().order_by("name")


class StationTagViewSet(AreaReadOnlyViewSet):
    area = "stations"
    serializer_class = StationTagSerializer
    search_fields = ["name", "slug"]
    ordering_fields = ["name"]

    def get_queryset(self):
        return StationTag.objects.all().order_by("name")


User = get_user_model()


def _assignments_visible_to(qs, viewer):
    """Narrow assignment rows to those whose target user the viewer may see.

    Mirrors ``visibility.audience_for`` plus the directory filter:

    - ``users.manage`` holders see every row.
    - Applicant viewers see only their own rows (``audience_for`` gives an
      Applicant no view of any other user).
    - Everyone else sees their own rows plus rows of users who are
      directory-visible (``is_directory_visible``), active, not soft-deleted
      and not Applicants.
    """
    if can(viewer, "users", "manage"):
        return qs
    own = Q(user=viewer)
    if level_of(viewer) == Level.APPLICANT:
        return qs.filter(own)
    visible = own | Q(
        user__is_directory_visible=True,
        user__is_active=True,
        user__deleted_at__isnull=True,
    ) & ~Q(user__membership_level=User.MembershipLevel.APPLICANT)
    return qs.filter(visible)


class StationAssignmentViewSet(AreaReadOnlyViewSet):
    area = "topology"
    serializer_class = StationAssignmentSerializer
    filterset_fields = ["station", "user", "role"]
    ordering_fields = ["assigned_at"]

    def get_queryset(self):
        return _assignments_visible_to(
            StationAssignment.objects.all(), self.request.user
        ).order_by("-assigned_at")


class RegionAssignmentViewSet(AreaReadOnlyViewSet):
    area = "topology"
    serializer_class = RegionAssignmentSerializer
    filterset_fields = ["region", "user", "role"]
    ordering_fields = ["assigned_at"]

    def get_queryset(self):
        return _assignments_visible_to(RegionAssignment.objects.all(), self.request.user).order_by(
            "-assigned_at"
        )


class RolloutSequenceViewSet(AreaReadOnlyViewSet):
    area = "rollouts"
    serializer_class = RolloutSequenceSerializer

    def get_queryset(self):
        return RolloutSequence.objects.all().order_by("id")

    @action(detail=True, url_path="entries")
    def entries(self, request, pk=None):
        seq = self.get_object()
        return self._child_list(
            request, seq.entries.order_by("position"), RolloutSequenceEntrySerializer
        )


class RolloutSequenceEntryViewSet(AreaReadOnlyViewSet):
    area = "rollouts"
    serializer_class = RolloutSequenceEntrySerializer
    filterset_fields = ["sequence", "group"]
    ordering_fields = ["position"]

    def get_queryset(self):
        return RolloutSequenceEntry.objects.all().order_by("position")


class DeploymentViewSet(AreaReadOnlyViewSet):
    area = "deployments"
    serializer_class = DeploymentSerializer
    filterset_class = DeploymentFilter
    ordering_fields = ["created_at", "updated_at"]

    def get_queryset(self):
        return Deployment.objects.all().order_by("-created_at")

    def get_serializer(self, *args, **kwargs):
        if kwargs.get("many"):
            from apps.deployments.models import bulk_compute_progress

            instances = args[0] if args else kwargs.get("instance")
            ids = [obj.pk for obj in instances]
            context = kwargs.pop("context", None) or self.get_serializer_context()
            context["deployment_progress_map"] = bulk_compute_progress(
                DeploymentResult.objects.all(), ids
            )
            kwargs["context"] = context
        return super().get_serializer(*args, **kwargs)

    @action(detail=True, url_path="results")
    def results(self, request, pk=None):
        dep = self.get_object()
        qs = DeploymentResult.objects.filter(deployment=dep).order_by("station_id")
        return self._child_list(request, qs, DeploymentResultSerializer)


class DeploymentResultViewSet(AreaReadOnlyViewSet):
    area = "deployments"
    serializer_class = DeploymentResultSerializer
    filterset_class = DeploymentResultFilter
    ordering_fields = ["started_at", "completed_at"]

    def get_queryset(self):
        return DeploymentResult.objects.all().order_by("-id")


class AlertRuleViewSet(AreaReadOnlyViewSet):
    area = "alert_rules"
    serializer_class = AlertRuleSerializer
    filterset_fields = ["alert_type", "severity", "is_active"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        from apps.monitoring.models import AlertRule

        return AlertRule.objects.all().order_by("alert_type")


class AlertViewSet(AreaReadOnlyViewSet):
    area = "alerts"
    serializer_class = AlertSerializer
    filterset_class = AlertFilter
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return Alert.objects.all().order_by("-created_at")


class ProvisioningJobViewSet(AreaReadOnlyViewSet):
    area = "provisioning"
    serializer_class = ProvisioningJobSerializer
    filterset_class = ProvisioningJobFilter
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return ProvisioningJob.objects.all().order_by("-created_at")


class ImageReleaseViewSet(AreaReadOnlyViewSet):
    area = "images"
    serializer_class = ImageReleaseSerializer
    filterset_class = ImageReleaseFilter
    search_fields = ["tag"]
    ordering_fields = ["imported_at", "tag"]

    def get_queryset(self):
        return ImageRelease.objects.all().order_by("-imported_at")


class ImageImportJobViewSet(AreaReadOnlyViewSet):
    area = "images"
    serializer_class = ImageImportJobSerializer
    filterset_fields = ["machine", "status", "channel"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return ImageImportJob.objects.all().order_by("-created_at")


class UserViewSet(AreaReadOnlyViewSet):
    """User directory (``users.view``; writes ``users.manage``).

    Returns exactly the set the UI member directory shows the viewer
    (``visibility.directory_queryset``): Members get active, non-Applicant
    users; managers every non-deleted user. Fields per target are filtered by
    ``UserSerializer`` via ``directory_visible_fields``.
    """

    area = "users"
    serializer_class = UserSerializer

    def _viewer_manages_users(self):
        request = getattr(self, "request", None)
        return request is not None and can(request.user, "users", "manage")

    # Non-managers may only search/order by ``username``: every directory row
    # shows it, whereas email/names/join date are hidden for some rows
    # (``is_directory_visible=False`` → minimal fields), and a substring search
    # or sort on them would let a Member probe the hidden values. Same rule as
    # the UI ``UserListView`` search.
    @property
    def search_fields(self):
        if self._viewer_manages_users():
            return ["username", "email", "first_name", "last_name"]
        return ["username"]

    @property
    def ordering_fields(self):
        if self._viewer_manages_users():
            return ["username", "date_joined"]
        return ["username"]

    def get_queryset(self):
        return directory_queryset(self.request.user).order_by("username")

    def get_object(self):
        obj = super().get_object()
        # Defense in depth: a target the viewer has no audience for is
        # invisible (404, no existence leak), never a 403 or an empty body.
        if audience_for(self.request.user, obj) is None:
            raise NotFound
        return obj
