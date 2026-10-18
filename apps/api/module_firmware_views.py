"""Module firmware viewsets for the user/automation API (area ``modules``).

Reads need ``modules.view``; no topology scoping (same as the GUI).
"""

from django.db.models import Count, Prefetch

from apps.api.module_firmware_serializers import (
    ModuleFirmwareImportJobSerializer,
    ModuleFirmwareReleaseSerializer,
    ModuleFirmwareTargetSerializer,
    ModuleSerializer,
    ModuleTypeSerializer,
)
from apps.api.read_filters import (
    ModuleFilter,
    ModuleFirmwareImportJobFilter,
    ModuleFirmwareReleaseFilter,
    ModuleFirmwareTargetFilter,
)
from apps.api.read_views import AreaReadOnlyViewSet
from apps.module_firmware import services
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)


class ModuleTypeViewSet(AreaReadOnlyViewSet):
    area = "modules"
    serializer_class = ModuleTypeSerializer
    search_fields = ["key", "display_name"]
    ordering_fields = ["key", "display_name", "created_at"]

    def get_queryset(self):
        # Same Counts as the GUI type list (ModuleTypeListView).
        return ModuleType.objects.annotate(
            module_count=Count("modules", distinct=True),
            release_count=Count("firmware_releases", distinct=True),
            target_count=Count("firmware_targets", distinct=True),
        ).order_by("key")


class ModuleViewSet(AreaReadOnlyViewSet):
    area = "modules"
    serializer_class = ModuleSerializer
    filterset_class = ModuleFilter
    search_fields = ["uid"]
    ordering_fields = ["uid", "last_seen", "first_seen"]

    def get_queryset(self):
        # Same prefetches as ConvergenceOverviewView so firmware_row runs
        # without per-row assignment/quarantine queries.
        open_assignments = ModuleAssignmentHistory.objects.filter(
            to_ts__isnull=True
        ).select_related("station")
        return (
            Module.objects.select_related("module_type")
            .prefetch_related(
                Prefetch("assignments", queryset=open_assignments, to_attr="open_assignments"),
                Prefetch(
                    "convergence_states",
                    queryset=services.quarantined_states_qs(),
                    to_attr="quarantined_states",
                ),
            )
            .order_by("module_type__key", "uid")
        )


class ModuleFirmwareReleaseViewSet(AreaReadOnlyViewSet):
    area = "modules"
    serializer_class = ModuleFirmwareReleaseSerializer
    filterset_class = ModuleFirmwareReleaseFilter
    search_fields = ["version", "source_tag"]
    ordering_fields = ["imported_at", "version"]

    def get_queryset(self):
        # all_objects: the filterset hides archived rows unless
        # include_archived=true.
        return ModuleFirmwareRelease.all_objects.select_related(
            "module_type", "imported_by"
        ).order_by("module_type__key", "variant", "-version")


class ModuleFirmwareImportJobViewSet(AreaReadOnlyViewSet):
    area = "modules"
    serializer_class = ModuleFirmwareImportJobSerializer
    filterset_class = ModuleFirmwareImportJobFilter
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return ModuleFirmwareImportJob.objects.select_related("requested_by").order_by(
            "-created_at"
        )


class ModuleFirmwareTargetViewSet(AreaReadOnlyViewSet):
    area = "modules"
    serializer_class = ModuleFirmwareTargetSerializer
    filterset_class = ModuleFirmwareTargetFilter
    ordering_fields = ["created_at", "updated_at"]

    def get_queryset(self):
        return ModuleFirmwareTarget.objects.select_related("module_type", "created_by").order_by(
            "module_type__key", "scope", "pk"
        )
