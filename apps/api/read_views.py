"""Read-only viewsets for the user/automation API (v1)."""

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
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
    MetricDefinitionSerializer,
    MetricHistoryQuerySerializer,
    MetricHistorySerializer,
    ProvisioningJobSerializer,
    RegionAssignmentSerializer,
    RegionSerializer,
    RolloutGroupSerializer,
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
    RolloutGroup,
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

    @extend_schema(responses=StationTelemetrySerializer)
    @action(detail=True, url_path="telemetry")
    def telemetry(self, request, pk=None):
        from apps.stations.models import StationTelemetry

        station = self.get_object()
        obj = StationTelemetry.objects.filter(station=station).first()
        if obj is None:
            raise NotFound()
        return Response(StationTelemetrySerializer(obj, context={"request": request}).data)

    @extend_schema(parameters=[MetricHistoryQuerySerializer], responses=MetricHistorySerializer)
    @action(detail=True, url_path="metrics/history", url_name="metrics-history")
    def metrics_history(self, request, pk=None):
        """Downsampled history of the station's generic metrics.

        Same permissions as ``/stations/{id}/telemetry/``. Each series point is
        ``[epoch_seconds, min, avg, max]`` (UTC, sorted). ``since``/``until`` are ISO 8601
        (naive = UTC; default: the last 24 h); the response echoes them in UTC (``Z``).

        Resolutions: ``raw`` (one point per heartbeat) and ``5m`` come from the raw table
        (kept ``HISTORY_RAW_RETENTION_DAYS``, default 30 d); if ``since`` is older than that,
        they are served as ``1h`` instead (the response reports the effective ``resolution``
        and ``source``). ``1h`` and ``1d`` (count-weighted daily average) come from the
        hourly table (kept ``HISTORY_HOURLY_RETENTION_DAYS``, default 730 d); the current,
        not yet compacted hour is not in there yet.

        Limits: at most 10 keys per request; time range at most
        ``HISTORY_HOURLY_RETENTION_DAYS`` days; ``resolution=auto`` (default) picks the
        finest resolution with at most 1000 points per series; an explicit resolution may
        yield at most 5000 points per series, otherwise 400. Point counts for ``raw`` are
        estimated at the shortest heartbeat interval (10 s). Validation errors are 400 with
        field-keyed messages.

        Without ``keys``: the station's current metrics with a visible (non-hidden,
        non-blocked) definition. Like ``/telemetry/``, hidden or blocked keys are never
        returned — they are silently dropped from an explicit ``keys`` list too. Undefined
        explicit keys are returned (``label=key``, ``unit=""``, ``precision=1``,
        ``group="other"``).
        """
        from apps.history import query as history_query
        from apps.monitoring.models import MetricDefinition

        station = self.get_object()
        params = MetricHistoryQuerySerializer(
            data=request.query_params, context={"now": timezone.now()}
        )
        params.is_valid(raise_exception=True)
        q = params.validated_data
        keys = (
            history_query.visible_keys(q["keys"])
            if q["keys"] is not None
            else history_query.default_keys(station)
        )
        points = history_query.fetch_series(
            station, keys, q["since"], q["until"], q["resolution"], q["source"]
        )
        defs = {d.key: d for d in MetricDefinition.objects.filter(key__in=keys)}
        series = []
        for key in keys:
            d = defs.get(key)
            series.append(
                {
                    "key": key,
                    "label": d.display_label if d else key,
                    "unit": d.unit if d else "",
                    "precision": d.precision if d else 1,
                    "group": d.group if d else "other",
                    "points": points[key],
                }
            )
        payload = {
            "station": station.pk,
            "since": q["since"],
            "until": q["until"],
            "resolution": q["resolution"],
            "source": q["source"],
            "series": series,
        }
        return Response(MetricHistorySerializer(payload).data)

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


class RolloutGroupViewSet(AreaReadOnlyViewSet):
    area = "stations"
    serializer_class = RolloutGroupSerializer
    search_fields = ["name", "slug"]
    ordering_fields = ["name"]

    def get_queryset(self):
        return RolloutGroup.objects.all().order_by("name")


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


class MetricDefinitionViewSet(AreaReadOnlyViewSet):
    """Agent-described metric definitions (read: ``alert_rules.view``)."""

    area = "alert_rules"
    serializer_class = MetricDefinitionSerializer
    filterset_fields = ["group", "is_hidden", "is_blocked"]
    search_fields = ["key", "label", "label_override"]
    ordering_fields = ["key", "group", "last_seen"]

    def get_queryset(self):
        from apps.monitoring.models import MetricDefinition

        return MetricDefinition.objects.all().order_by("group", "key")


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
