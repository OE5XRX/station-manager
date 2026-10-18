"""Module firmware viewsets for the user/automation API (area ``modules``).

Reads need ``modules.view``, writes ``modules.manage``; no topology scoping
(same as the GUI). Every mutation audits via the GUI helpers plus token origin.
"""

from django.db import transaction
from django.db.models import Count, Prefetch
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as rf_serializers
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.mixins import CreateModelMixin, DestroyModelMixin, UpdateModelMixin
from rest_framework.response import Response

from apps.accounts.client_ip import client_ip
from apps.api.audit import _token_suffix
from apps.api.module_firmware_serializers import (
    ModuleFirmwareImportInputSerializer,
    ModuleFirmwareImportJobSerializer,
    ModuleFirmwareReleaseSerializer,
    ModuleFirmwareTargetSerializer,
    ModuleFirmwareTargetWriteSerializer,
    ModuleSerializer,
    ModuleTypeSerializer,
    ModuleTypeWriteSerializer,
    ModuleUpdateSerializer,
)
from apps.api.pagination import StandardResultsSetPagination
from apps.api.read_filters import (
    ModuleFilter,
    ModuleFirmwareImportJobFilter,
    ModuleFirmwareReleaseFilter,
    ModuleFirmwareTargetFilter,
)
from apps.api.read_views import AreaReadOnlyViewSet
from apps.api.write_views import AreaWriteViewSet
from apps.images import github_releases
from apps.module_firmware import ops, services
from apps.module_firmware.audit import audit_config, audit_station
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.trust import import_blockers
from apps.module_firmware.views_convergence import is_int_param
from apps.module_firmware.views_types import type_in_use
from apps.stations.models import StationAuditLog


def _conflict(detail):
    err = APIException(detail=detail)
    err.status_code = http_status.HTTP_409_CONFLICT
    return err


def _bad_gateway(detail):
    err = APIException(detail=detail)
    err.status_code = http_status.HTTP_502_BAD_GATEWAY
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


class ModulePagination(StandardResultsSetPagination):
    # firmware_row resolves the effective target per row (a few queries each),
    # so cap the page at the GUI convergence overview's size instead of 200.
    max_page_size = 50


class ModuleViewSet(AreaReadOnlyViewSet):
    area = "modules"
    pagination_class = ModulePagination
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

    # Writes: archive/restore + the GitHub import pair below. No create/PUT/
    # DELETE — releases only come from the importer (DELETE → 405).

    def _release_data(self, release):
        return ModuleFirmwareReleaseSerializer(release, context=self.get_serializer_context()).data

    @extend_schema(request=None, responses={200: ModuleFirmwareReleaseSerializer})
    @action(detail=True, methods=["post"], url_path="archive", verb="manage")
    def archive(self, request, pk=None):
        """Archive a release (hidden from targets/convergence). Idempotent:
        audit + reconcile only when the state actually changed."""
        release = self.get_object()
        ops.archive_release(request, release, via=_token_suffix(request))
        return Response(self._release_data(release))

    @extend_schema(request=None, responses={200: ModuleFirmwareReleaseSerializer})
    @action(detail=True, methods=["post"], url_path="restore", verb="manage")
    def restore(self, request, pk=None):
        """Undo an archive. Idempotent (no audit on a no-op)."""
        release = self.get_object()
        ops.restore_release(request, release, via=_token_suffix(request))
        return Response(self._release_data(release))

    @staticmethod
    def _require_import_ready(module_type):
        blockers = import_blockers(module_type)
        if blockers:
            raise ValidationError({"module_type": blockers})

    @staticmethod
    def _github_rows(module_type):
        try:
            return ops.github_release_rows(module_type)
        except github_releases.GitHubAPIError as exc:
            raise _bad_gateway(f"GitHub releases unavailable: {exc}") from exc

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "module_type", OpenApiTypes.INT, OpenApiParameter.QUERY, required=True
            )
        ],
        responses={
            200: inline_serializer(
                "ModuleFirmwareGitHubRelease",
                fields={
                    "tag": rf_serializers.CharField(),
                    "html_url": rf_serializers.URLField(),
                    "is_latest": rf_serializers.BooleanField(),
                    "state": rf_serializers.ChoiceField(
                        choices=["ready", "queued", "imported", "no_assets", "invalid_tag"]
                    ),
                    "variants": rf_serializers.ListField(child=rf_serializers.CharField()),
                    "missing_variants": rf_serializers.ListField(child=rf_serializers.CharField()),
                },
                many=True,
            )
        },
    )
    @action(detail=False, methods=["get"], url_path="available", verb="manage")
    def available(self, request):
        """GitHub releases of the module type's firmware repo with their import
        state (only ``ready`` rows can be imported). ``missing_variants`` lists
        fleet variants a release has no asset for. Only the newest releases
        returned by ``github_releases.fetch_releases`` (its default limit) are
        listed; older tags are not considered."""
        module_type_id = request.query_params.get("module_type", "")
        if not is_int_param(module_type_id):
            raise ValidationError({"module_type": ["A module type id is required."]})
        module_type = get_object_or_404(ModuleType, pk=module_type_id)
        self._require_import_ready(module_type)
        data = [
            {
                "tag": row["release"].tag,
                "html_url": row["release"].html_url,
                "is_latest": row["release"].is_latest,
                "state": row["state"],
                "variants": row["variants"],
                "missing_variants": row["missing_variants"],
            }
            for row in self._github_rows(module_type)
        ]
        return Response(data)

    @extend_schema(
        request=ModuleFirmwareImportInputSerializer,
        responses={202: ModuleFirmwareImportJobSerializer},
    )
    @action(detail=False, methods=["post"], url_path="import", url_name="import", verb="manage")
    def import_release(self, request):
        """Queue an import job for a ``ready`` GitHub release tag (see
        ``available/``). 409 when the tag is already imported or in flight.
        Only the newest releases returned by ``github_releases.fetch_releases``
        (its default limit) are considered; an older tag is "not found"."""
        serializer = ModuleFirmwareImportInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        module_type_id = serializer.validated_data["module_type"]
        tag = serializer.validated_data["tag"]

        module_type = ModuleType.objects.filter(pk=module_type_id).first()
        if module_type is None:
            raise ValidationError({"module_type": ["Unknown module type."]})
        self._require_import_ready(module_type)

        row = next((r for r in self._github_rows(module_type) if r["release"].tag == tag), None)
        if row is None:
            raise ValidationError({"tag": [f"Tag '{tag}' not found in the GitHub releases."]})
        state = row["state"]
        if state == "invalid_tag":
            raise ValidationError({"tag": ["Invalid release tag."]})
        if state == "no_assets":
            raise ValidationError({"tag": ["Release has no importable firmware assets."]})
        if state == "imported":
            raise _conflict(f"Release {tag} is already imported.")
        if state == "queued":
            raise _conflict(f"An import of {tag} is already in progress.")

        try:
            job, created = ops.queue_import(
                request, module_type_id, tag, via=_token_suffix(request)
            )
        except ops.ImportRejected as exc:
            raise ValidationError({exc.field: [str(exc)]}) from exc
        except ModuleType.DoesNotExist as exc:
            raise ValidationError({"module_type": ["Unknown module type."]}) from exc
        if not created:
            raise _conflict(f"An import of {tag} is already in progress.")
        data = ModuleFirmwareImportJobSerializer(job, context=self.get_serializer_context()).data
        return Response(data, status=http_status.HTTP_202_ACCEPTED)


class ModuleFirmwareImportJobViewSet(AreaReadOnlyViewSet):
    area = "modules"
    serializer_class = ModuleFirmwareImportJobSerializer
    filterset_class = ModuleFirmwareImportJobFilter
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return ModuleFirmwareImportJob.objects.select_related("requested_by").order_by(
            "-created_at"
        )


class ModuleFirmwareTargetViewSet(
    AreaWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    AreaReadOnlyViewSet,
):
    area = "modules"
    serializer_class = ModuleFirmwareTargetSerializer
    write_serializer_class = ModuleFirmwareTargetWriteSerializer
    filterset_class = ModuleFirmwareTargetFilter
    ordering_fields = ["created_at", "updated_at"]

    # Same related rows the GUI target views load (audit details/labels).
    _AUDIT_RELATED = ("module_type", "tag", "station", "canary_tag")

    def get_queryset(self):
        return ModuleFirmwareTarget.objects.select_related(
            *self._AUDIT_RELATED, "created_by"
        ).order_by("module_type__key", "scope", "pk")

    def _read_response(self, obj, status):
        obj = self.get_queryset().get(pk=obj.pk)
        data = ModuleFirmwareTargetSerializer(obj, context=self.get_serializer_context()).data
        return Response(data, status=status)

    @extend_schema(
        request=ModuleFirmwareTargetWriteSerializer,
        responses={201: ModuleFirmwareTargetSerializer},
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return self._read_response(serializer.instance, http_status.HTTP_201_CREATED)

    @extend_schema(
        request=ModuleFirmwareTargetWriteSerializer,
        responses={200: ModuleFirmwareTargetSerializer},
    )
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return self._read_response(serializer.instance, http_status.HTTP_200_OK)

    @extend_schema(
        request=ModuleFirmwareTargetWriteSerializer,
        responses={200: ModuleFirmwareTargetSerializer},
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    def perform_create(self, serializer):
        target = serializer.save(created_by=self.request.user)
        ops.after_target_created(self.request, target, via=_token_suffix(self.request))

    def perform_update(self, serializer):
        # Fresh snapshot: the form already wrote the new values onto
        # serializer.instance during validation.
        before = ModuleFirmwareTarget.objects.select_related(*self._AUDIT_RELATED).get(
            pk=serializer.instance.pk
        )
        target = serializer.save()
        # No-op (no audited field changed) → no audit, no reconcile.
        ops.after_target_updated(self.request, before, target, via=_token_suffix(self.request))

    def perform_destroy(self, instance):
        details = ops.target_details(instance)
        label = ops.target_label(instance)
        instance.delete()
        ops.after_target_deleted(
            self.request, instance, details, label, via=_token_suffix(self.request)
        )
