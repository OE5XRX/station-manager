"""Explicit read serializers for the module firmware API (area ``modules``).

No ``fields="__all__"``: release storage keys (``storage_key``,
``cosign_bundle_key``) are structurally absent.
"""

from collections import defaultdict

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.trust import import_blockers
from apps.module_firmware.views_convergence import firmware_row


def _username_field():
    return serializers.SlugRelatedField(slug_field="username", read_only=True)


class ModuleTypeSerializer(serializers.ModelSerializer):
    import_blockers = serializers.SerializerMethodField()
    # Annotated by the viewset (same Counts as the GUI type list).
    module_count = serializers.IntegerField(read_only=True)
    release_count = serializers.IntegerField(read_only=True)
    target_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ModuleType
        fields = [
            "id",
            "key",
            "display_name",
            "hw_repo",
            "firmware_repo",
            "release_asset_prefix",
            "import_blockers",
            "module_count",
            "release_count",
            "target_count",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_import_blockers(self, obj):
        return [str(r) for r in import_blockers(obj)]


class _AssignmentSerializer(serializers.Serializer):
    station = serializers.IntegerField(source="station_id", allow_null=True)
    station_name = serializers.CharField(source="station.name", allow_null=True, default=None)
    slot = serializers.CharField()
    since = serializers.DateTimeField(source="from_ts")


class _QuarantineSerializer(serializers.Serializer):
    target_version = serializers.CharField(source="target_release.version")
    attempts = serializers.IntegerField()
    last_error_mode = serializers.CharField()
    last_error_message = serializers.CharField()
    updated_at = serializers.DateTimeField()


class ModuleListSerializer(serializers.ListSerializer):
    """Computes the page's active release keys once (one query) instead of an
    EXISTS per row — same optimisation as the GUI convergence overview."""

    def to_representation(self, data):
        items = list(data.all() if hasattr(data, "all") else data)
        self.child.release_keys = set(
            ModuleFirmwareRelease.objects.filter(
                module_type_id__in={m.module_type_id for m in items}
            ).values_list("module_type_id", "variant", "version")
        )
        return super().to_representation(items)


class ModuleSerializer(serializers.ModelSerializer):
    """Module + firmware facts (``views_convergence.firmware_row``).

    Expects the viewset's ``open_assignments`` / ``quarantined_states``
    prefetches (falls back to queries without them)."""

    module_type_key = serializers.CharField(source="module_type.key", read_only=True)
    assignment = serializers.SerializerMethodField()
    target_version = serializers.SerializerMethodField()
    has_release = serializers.SerializerMethodField()
    quarantine = serializers.SerializerMethodField()

    # Set by ModuleListSerializer for list pages; None → per-object EXISTS.
    release_keys = None

    class Meta:
        model = Module
        list_serializer_class = ModuleListSerializer
        fields = [
            "id",
            "uid",
            "uid_source",
            "module_type",
            "module_type_key",
            "variant",
            "lifecycle_status",
            "registration_status",
            "last_reported_version",
            "firmware_convergence",
            "first_seen",
            "last_seen",
            "notes",
            "assignment",
            "target_version",
            "has_release",
            "quarantine",
        ]

    def to_representation(self, instance):
        open_rows = getattr(instance, "open_assignments", None)
        if open_rows is None:
            open_rows = list(
                ModuleAssignmentHistory.objects.filter(
                    module=instance, to_ts__isnull=True
                ).select_related("station")
            )
        self._assignment = open_rows[0] if open_rows else None
        self._facts = firmware_row(instance, self._assignment, release_keys=self.release_keys)
        return super().to_representation(instance)

    @extend_schema_field(_AssignmentSerializer(allow_null=True))
    def get_assignment(self, obj):
        if self._assignment is None:
            return None
        return _AssignmentSerializer(self._assignment).data

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_target_version(self, obj):
        target = self._facts["target"]
        return target.version if target is not None else None

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_release(self, obj):
        return self._facts["has_release"]

    @extend_schema_field(_QuarantineSerializer(allow_null=True))
    def get_quarantine(self, obj):
        state = self._facts["quarantine"]
        if state is None:
            return None
        return _QuarantineSerializer(state).data


class ModuleFirmwareReleaseSerializer(serializers.ModelSerializer):
    module_type_key = serializers.CharField(source="module_type.key", read_only=True)
    imported_by = _username_field()

    class Meta:
        model = ModuleFirmwareRelease
        fields = [
            "id",
            "module_type",
            "module_type_key",
            "variant",
            "version",
            "sha256",
            "size_bytes",
            "source_repo",
            "source_tag",
            "source_github_url",
            "imported_at",
            "imported_by",
            "archived_at",
        ]
        # excludes storage_key, cosign_bundle_key (infra storage)


class ModuleFirmwareImportJobSerializer(serializers.ModelSerializer):
    requested_by = _username_field()

    class Meta:
        model = ModuleFirmwareImportJob
        fields = [
            "id",
            "module_type",
            "source_repo",
            "tag",
            "status",
            "error_message",
            "release",
            "requested_by",
            "created_at",
            "completed_at",
        ]


def variant_coverage_map(type_ids):
    """(module_type_id, version) -> variants with an active release ("—" for
    blank). Mirrors ``TargetListView.get_context_data``."""
    coverage = defaultdict(list)
    for type_id, version, variant in ModuleFirmwareRelease.objects.filter(
        module_type_id__in=type_ids
    ).values_list("module_type_id", "version", "variant"):
        coverage[(type_id, version)].append(variant or "—")
    return coverage


class ModuleFirmwareTargetListSerializer(serializers.ListSerializer):
    """One coverage query per page instead of one per target."""

    def to_representation(self, data):
        items = list(data.all() if hasattr(data, "all") else data)
        self.child.coverage = variant_coverage_map({t.module_type_id for t in items})
        return super().to_representation(items)


class ModuleFirmwareTargetSerializer(serializers.ModelSerializer):
    module_type_key = serializers.CharField(source="module_type.key", read_only=True)
    variant_coverage = serializers.SerializerMethodField()
    created_by = _username_field()

    # Set by ModuleFirmwareTargetListSerializer; None → per-object query.
    coverage = None

    class Meta:
        model = ModuleFirmwareTarget
        list_serializer_class = ModuleFirmwareTargetListSerializer
        fields = [
            "id",
            "module_type",
            "module_type_key",
            "version",
            "scope",
            "tag",
            "station",
            "canary_tag",
            "variant_coverage",
            "created_by",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_variant_coverage(self, obj):
        coverage = self.coverage
        if coverage is None:
            coverage = variant_coverage_map({obj.module_type_id})
        return sorted(coverage.get((obj.module_type_id, obj.version), []))
