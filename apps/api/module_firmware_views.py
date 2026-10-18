"""Module firmware viewsets for the user/automation API (area ``modules``).

Reads need ``modules.view``, writes ``modules.manage``; no topology scoping
(same as the GUI). Every mutation audits via the GUI helpers plus token origin.
"""

from django.db import transaction
from django.db.models import Count, Prefetch
from django.db.models.deletion import ProtectedError
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as rf_serializers
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.mixins import CreateModelMixin, DestroyModelMixin, UpdateModelMixin
from rest_framework.response import Response

from apps.accounts.client_ip import client_ip
from apps.api.audit import _token_suffix
from apps.api.module_firmware_serializers import (
    ModuleFirmwareImportJobSerializer,
    ModuleFirmwareReleaseSerializer,
    ModuleFirmwareTargetSerializer,
    ModuleSerializer,
    ModuleTypeSerializer,
    ModuleTypeWriteSerializer,
    ModuleUpdateSerializer,
)
from apps.api.read_filters import (
    ModuleFilter,
    ModuleFirmwareImportJobFilter,
    ModuleFirmwareReleaseFilter,
    ModuleFirmwareTargetFilter,
)
from apps.api.read_views import AreaReadOnlyViewSet
from apps.api.write_views import AreaWriteViewSet
from apps.module_firmware import services
from apps.module_firmware.audit import audit_config, audit_station
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.views_types import type_in_use
from apps.stations.models import StationAuditLog


def _conflict(detail):
    err = APIException(detail=detail)
    err.status_code = http_status.HTTP_409_CONFLICT
    return err


class ModuleTypeViewSet(
    AreaWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    AreaReadOnlyViewSet,
):
    area = "modules"
    serializer_class = ModuleTypeSerializer
    write_serializer_class = ModuleTypeWriteSerializer
    search_fields = ["key", "display_name"]
    ordering_fields = ["key", "display_name", "created_at"]

    def get_queryset(self):
        # Same Counts as the GUI type list (ModuleTypeListView).
        return ModuleType.objects.annotate(
            module_count=Count("modules", distinct=True),
            release_count=Count("firmware_releases", distinct=True),
            target_count=Count("firmware_targets", distinct=True),
        ).order_by("key")

    def _read_response(self, obj, status):
        """Respond with the read shape (import_blockers + annotated counts)."""
        obj = self.get_queryset().get(pk=obj.pk)
        data = ModuleTypeSerializer(obj, context=self.get_serializer_context()).data
        return Response(data, status=status)

    @extend_schema(request=ModuleTypeWriteSerializer, responses={201: ModuleTypeSerializer})
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return self._read_response(serializer.instance, http_status.HTTP_201_CREATED)

    @extend_schema(request=ModuleTypeWriteSerializer, responses={200: ModuleTypeSerializer})
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return self._read_response(serializer.instance, http_status.HTTP_200_OK)

    @extend_schema(request=ModuleTypeWriteSerializer, responses={200: ModuleTypeSerializer})
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    def perform_create(self, serializer):
        obj = serializer.save()
        audit_config(
            self.request,
            obj="module_type",
            action="created",
            message=f"Module type {obj.key} created" + _token_suffix(self.request),
            details={"key": obj.key},
        )

    def perform_update(self, serializer):
        form = serializer.form
        if not form.has_changed():
            # Nothing changed: save is harmless, but no audit row for a no-op.
            serializer.save()
            return
        changes = {
            f: {"old": str(form.initial.get(f, "")), "new": str(form.cleaned_data.get(f, ""))}
            for f in form.changed_data
        }
        obj = serializer.save()
        audit_config(
            self.request,
            obj="module_type",
            action="updated",
            message=f"Module type {obj.key} updated" + _token_suffix(self.request),
            details={"key": obj.key, "changes": changes},
        )

    def perform_destroy(self, instance):
        in_use = _conflict(
            "This module type is in use by modules, releases or targets and cannot be deleted."
        )
        if type_in_use(instance):
            raise in_use
        key = instance.key
        try:
            instance.delete()
        except ProtectedError as exc:
            raise in_use from exc
        audit_config(
            self.request,
            obj="module_type",
            action="deleted",
            message=f"Module type {key} deleted" + _token_suffix(self.request),
            details={"key": key},
        )


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

    # Writes: PATCH (notes/lifecycle) and the two detail actions below. No
    # create/PUT/delete — modules come from station inventory ingest.

    def _read_data(self, module):
        """Fresh read shape (re-fetched with the list prefetches)."""
        module = self.get_queryset().get(pk=module.pk)
        return ModuleSerializer(module, context=self.get_serializer_context()).data

    def _service_kwargs(self):
        return {
            "user": self.request.user,
            "ip_address": client_ip(self.request),
            "audit_suffix": _token_suffix(self.request),
        }

    @extend_schema(request=ModuleUpdateSerializer, responses={200: ModuleSerializer})
    def partial_update(self, request, *args, **kwargs):
        module = self.get_object()
        serializer = ModuleUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        old_notes = module.notes
        notes_changed = "notes" in data and data["notes"] != old_notes
        try:
            # Atomic: a rejected lifecycle change leaves notes untouched too.
            with transaction.atomic():
                if "lifecycle_status" in data:
                    services.set_lifecycle(
                        module, data["lifecycle_status"], **self._service_kwargs()
                    )
                if notes_changed:
                    module.notes = data["notes"]
                    module.save(update_fields=["notes", "updated_at"])
        except ValueError as exc:
            raise ValidationError({"lifecycle_status": [str(exc)]}) from exc
        if notes_changed:
            assignment = (
                ModuleAssignmentHistory.objects.filter(module=module, to_ts__isnull=True)
                .select_related("station")
                .first()
            )
            audit_station(
                request,
                event_type=StationAuditLog.EventType.UPDATED,
                module=module,
                station=assignment.station if assignment else None,
                message=f"Notes updated for module {module.uid}" + _token_suffix(request),
                changes={"notes": {"old": old_notes, "new": module.notes}},
            )
        return Response(self._read_data(module))

    @extend_schema(request=None, responses={200: ModuleSerializer})
    @action(detail=True, methods=["post"], url_path="confirm-registration", verb="manage")
    def confirm_registration(self, request, pk=None):
        """Mark the module as registered. Idempotent (no audit on a no-op)."""
        module = self.get_object()
        services.confirm_registration(module, **self._service_kwargs())
        return Response(self._read_data(module))

    @extend_schema(
        request=None,
        responses={
            200: inline_serializer(
                "ModuleQuarantineRelease",
                fields={
                    "released_targets": rf_serializers.ListField(child=rf_serializers.CharField()),
                    "module": ModuleSerializer(),
                },
            )
        },
    )
    @action(detail=True, methods=["post"], url_path="release-quarantine", verb="manage")
    def release_quarantine(self, request, pk=None):
        """Release the live quarantine for retry. ``released_targets`` is empty
        (and nothing is audited) when the module is not quarantined."""
        module = self.get_object()
        versions = services.release_quarantine(module, **self._service_kwargs())
        return Response({"released_targets": versions, "module": self._read_data(module)})


class ModuleFirmwareReleaseViewSet(AreaReadOnlyViewSet):
    area = "modules"
    serializer_class = ModuleFirmwareReleaseSerializer
    filterset_class = ModuleFirmwareReleaseFilter
    search_fields = ["version", "source_tag"]
    ordering_fields = ["imported_at", "version"]

    def get_queryset(self):
        # all_objects: the list's filterset hides archived rows unless
        # include_archived=true; detail routes resolve archived ids too.
        return ModuleFirmwareRelease.all_objects.select_related(
            "module_type", "imported_by"
        ).order_by("module_type__key", "variant", "-version")

    def filter_queryset(self, queryset):
        # Filters (incl. the archived default) are a list concern; a release
        # id must stay addressable after archiving (detail, restore).
        if self.action != "list":
            return queryset
        return super().filter_queryset(queryset)


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
