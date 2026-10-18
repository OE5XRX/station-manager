"""Explicit serializers for the module firmware API (area ``modules``).

No ``fields="__all__"``: release storage keys (``storage_key``,
``cosign_bundle_key``) are structurally absent.
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.module_firmware import ops
from apps.module_firmware.forms import ModuleTypeForm
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


class ModuleTypeWriteSerializer(serializers.ModelSerializer):
    """Create/update input; validation and save go through the GUI's
    ``ModuleTypeForm`` (repo/prefix shape, trusted owner, unique key, ``key``
    immutable on edit) so API and GUI cannot drift.

    After ``is_valid()``, ``self.form`` is the bound form (``has_changed()`` /
    ``changed_data`` / ``initial`` feed the update audit)."""

    _FIELDS = ("key", "display_name", "hw_repo", "firmware_repo", "release_asset_prefix")

    class Meta:
        model = ModuleType
        fields = ["key", "display_name", "hw_repo", "firmware_repo", "release_asset_prefix"]
        # The form is the single validator (incl. unique key); no duplicate
        # model-derived validators on the serializer side.
        extra_kwargs = {name: {"validators": []} for name in fields}

    def validate(self, attrs):
        data = {}
        if self.instance is not None:
            data = {name: getattr(self.instance, name) for name in self._FIELDS}
        data.update(attrs)
        form = ModuleTypeForm(data=data, instance=self.instance)
        if not form.is_valid():
            raise serializers.ValidationError(
                {
                    ("non_field_errors" if field == "__all__" else field): list(errors)
                    for field, errors in form.errors.items()
                }
            )
        self.form = form
        return form.cleaned_data

    def create(self, validated_data):
        return self.form.save()

    def update(self, instance, validated_data):
        return self.form.save()


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


class _Row:
    """Read-only view of ``obj`` plus computed attributes, so declared (typed)
    serializer fields can read API-only facts without mutating the instance
    or the serializer."""

    def __init__(self, obj, **computed):
        self._obj = obj
        self.__dict__.update(computed)

    def __getattr__(self, name):
        return getattr(self._obj, name)


class ModuleListSerializer(serializers.ListSerializer):
    """Computes the page's active release keys once (one query) instead of an
    EXISTS per row — same optimisation as the GUI convergence overview."""

    def to_representation(self, data):
        items = list(data.all() if hasattr(data, "all") else data)
        keys = ops.active_release_keys({m.module_type_id for m in items})
        return [self.child.to_representation(m, release_keys=keys) for m in items]


class ModuleSerializer(serializers.ModelSerializer):
    """Module + firmware facts (``views_convergence.firmware_row``).

    Expects the viewset's ``open_assignments`` / ``quarantined_states``
    prefetches (falls back to queries without them)."""

    module_type_key = serializers.CharField(source="module_type.key", read_only=True)
    assignment = _AssignmentSerializer(read_only=True, allow_null=True)
    target_version = serializers.CharField(read_only=True, allow_null=True)
    has_release = serializers.BooleanField(read_only=True)
    quarantine = _QuarantineSerializer(read_only=True, allow_null=True)

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

    def to_representation(self, instance, release_keys=None):
        """``release_keys``: page-level set from ModuleListSerializer; None →
        firmware_row's per-object EXISTS (retrieve)."""
        open_rows = getattr(instance, "open_assignments", None)
        if open_rows is None:
            open_rows = list(
                ModuleAssignmentHistory.objects.filter(
                    module=instance, to_ts__isnull=True
                ).select_related("station")
            )
        assignment = open_rows[0] if open_rows else None
        facts = firmware_row(instance, assignment, release_keys=release_keys)
        target = facts["target"]
        row = _Row(
            instance,
            assignment=assignment,
            target_version=target.version if target is not None else None,
            has_release=facts["has_release"],
            quarantine=facts["quarantine"],
        )
        return super().to_representation(row)


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


class ModuleFirmwareTargetListSerializer(serializers.ListSerializer):
    """One coverage query per page instead of one per target."""

    def to_representation(self, data):
        items = list(data.all() if hasattr(data, "all") else data)
        coverage = ops.variant_coverage_map({t.module_type_id for t in items})
        return [self.child.to_representation(t, coverage=coverage) for t in items]


class ModuleFirmwareTargetSerializer(serializers.ModelSerializer):
    module_type_key = serializers.CharField(source="module_type.key", read_only=True)
    # Variants with an active release at this version ("—" = blank variant).
    variant_coverage = serializers.ListField(child=serializers.CharField(), read_only=True)
    created_by = _username_field()

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

    def to_representation(self, instance, coverage=None):
        """``coverage``: page-level map from the list serializer; None →
        one query for this target's type (retrieve)."""
        if coverage is None:
            coverage = ops.variant_coverage_map({instance.module_type_id})
        key = (instance.module_type_id, instance.version)
        return super().to_representation(
            _Row(instance, variant_coverage=sorted(coverage.get(key, [])))
        )
