"""Explicit read-only serializers for the user/automation API (v1).

No ``fields="__all__"`` anywhere — sensitive fields are structurally absent.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.accounts.models import User
from apps.accounts.permissions import can
from apps.accounts.visibility import Audience, audience_for, directory_visible_fields
from apps.control.models import StationModule
from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageImportJob, ImageRelease
from apps.monitoring.models import Alert, AlertRule
from apps.provisioning.models import ProvisioningJob
from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.stations.inventory import inventory_for_viewer
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationInventory,
    StationLogEntry,
    StationPhoto,
    StationTag,
    StationTelemetry,
)


def _can_manage_stations(serializer) -> bool:
    """``stations.manage`` for the requesting user; fail closed without a request.

    Mirrors the UI, which shows station IPs and network inventory only to
    station managers (Staff).
    """
    request = serializer.context.get("request")
    user = getattr(request, "user", None)
    return user is not None and can(user, "stations", "manage")


class RegionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Region
        fields = ["id", "name", "slug", "description", "created_at"]


class StationTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationTag
        fields = ["id", "name", "slug", "color", "description", "created_at"]


class StationSerializer(serializers.ModelSerializer):
    is_online = serializers.BooleanField(read_only=True)
    # Not ``read_only`` so the OpenAPI response schema marks it optional: it is
    # omitted for callers without ``stations.manage`` (read-only viewset, never
    # used for input).
    last_ip_address = serializers.IPAddressField(
        required=False,
        allow_null=True,
        help_text="Only present for callers with stations.manage (Staff).",
    )

    class Meta:
        model = Station
        fields = [
            "id",
            "name",
            "callsign",
            "description",
            "location_name",
            "latitude",
            "longitude",
            "altitude",
            "hardware_revision",
            "tx_audio_ceiling_dbfs",
            "region",
            "tags",
            "notes",
            "current_os_version",
            "current_agent_version",
            "current_image_variant",
            "last_ip_address",
            "last_seen",
            "status",
            "current_image_release",
            "is_online",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["tx_audio_ceiling_dbfs"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not _can_manage_stations(self):
            data.pop("last_ip_address", None)
        return data


class StationTelemetrySerializer(serializers.ModelSerializer):
    class Meta:
        model = StationTelemetry
        fields = [
            "id",
            "station",
            "data",
            "boot_id",
            "boot_count",
            "last_reboot_reason",
            "last_reboot_at",
            "uptime_seconds",
            "undervoltage_now",
            "undervoltage_occurred",
            "throttled_now",
            "throttled_occurred",
            "active_slot",
            "image_version",
            "last_ota_result",
            "worst_life_time_pct",
            "worst_pre_eol",
            "io_error_count",
            "updated_at",
        ]  # excludes power_alerted_boot_id, alerted_io_error_count (internal alerting)


class StationInventorySerializer(serializers.ModelSerializer):
    class Meta:
        model = StationInventory
        fields = ["id", "station", "data", "updated_at"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # The agent's inventory carries ``network`` (interface IPs + MACs); the
        # UI never shows it to viewers, so only stations.manage gets it.
        data["data"] = inventory_for_viewer(data.get("data"), _can_manage_stations(self))
        return data


class StationLogEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = StationLogEntry
        fields = ["id", "station", "entry_type", "title", "message", "created_by", "created_at"]


class StationPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationPhoto
        fields = ["id", "station", "image", "caption", "uploaded_by", "uploaded_at"]


class StationModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationModule
        fields = [
            "id",
            "station",
            "slot",
            "module_id",
            "type",
            "model",
            "version",
            "tracked_module",
            "capability_descriptor",
            "last_state",
            "online",
            "last_seen",
            "created_at",
            "updated_at",
        ]


class StationAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationAssignment
        fields = ["id", "user", "station", "role", "assigned_at", "assigned_by"]


class RegionAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = RegionAssignment
        fields = ["id", "user", "region", "role", "assigned_at", "assigned_by"]


class RolloutSequenceEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = RolloutSequenceEntry
        fields = ["id", "sequence", "group", "position"]


class RolloutSequenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = RolloutSequence
        fields = ["id", "singleton_key", "created_at", "updated_at", "updated_by"]


class DeploymentResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeploymentResult
        fields = [
            "id",
            "deployment",
            "station",
            "status",
            "started_at",
            "completed_at",
            "error_message",
            "previous_version",
            "new_version",
        ]


class DeploymentSerializer(serializers.ModelSerializer):
    progress = serializers.SerializerMethodField()

    def get_progress(self, obj):
        progress_map = self.context.get("deployment_progress_map")
        if progress_map is not None and obj.pk in progress_map:
            return progress_map[obj.pk]
        # Detail / no-map fallback: single aggregate (1 query).
        from apps.deployments.models import compute_progress

        return compute_progress(DeploymentResult.objects.filter(deployment=obj))

    class Meta:
        model = Deployment
        fields = [
            "id",
            "image_release",
            "target_type",
            "target_group",
            "target_station",
            "strategy",
            "phase_config",
            "status",
            "created_by",
            "created_at",
            "updated_at",
            "progress",
        ]


class AlertRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertRule
        fields = [
            "id",
            "alert_type",
            "threshold",
            "severity",
            "is_active",
            "description",
            "created_at",
        ]


class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = [
            "id",
            "station",
            "alert_rule",
            "severity",
            "title",
            "message",
            "is_acknowledged",
            "acknowledged_by",
            "acknowledged_at",
            "is_resolved",
            "resolved_at",
            "created_at",
        ]


class ProvisioningJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProvisioningJob
        fields = [
            "id",
            "station",
            "image_release",
            "status",
            "error_message",
            "output_size_bytes",
            "created_at",
            "ready_at",
            "downloaded_at",
            "expires_at",
            "requested_by",
        ]  # excludes output_s3_key (infra storage)


class ImageReleaseSerializer(serializers.ModelSerializer):
    is_ota_ready = serializers.BooleanField(read_only=True)

    class Meta:
        model = ImageRelease
        fields = [
            "id",
            "tag",
            "machine",
            "channel",
            "sha256",
            "size_bytes",
            "rootfs_sha256",
            "rootfs_size_bytes",
            "is_latest",
            "is_ota_ready",
            "imported_at",
            "imported_by",
            "archived_at",
        ]
        # excludes s3_key, cosign_bundle_s3_key, rootfs_s3_key (infra storage)


class ImageImportJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageImportJob
        fields = [
            "id",
            "tag",
            "machine",
            "channel",
            "mark_as_latest",
            "status",
            "error_message",
            "image_release",
            "requested_by",
            "created_at",
            "completed_at",
        ]


class UserSerializer(serializers.ModelSerializer):
    """Read-only user profile, filtered per target like the member directory.

    Sensitive fields (password, last_login, Django admin flags, is_active,
    deleted_at, deleted_by) are structurally absent — not listed in ``fields``.
    Of the rest, only the keys ``visibility.directory_visible_fields(viewer,
    target)`` allows are emitted (``id`` always), so e.g. Staff no longer sees
    every user's address/phone. Without a request the serializer fails closed
    to ``id`` only.

    Join date: ``date_joined_year`` (int) is emitted whenever the directory
    shows the join year; the full ``date_joined`` timestamp only to Admins and
    the user themself — Members see peers' join *year* only, like the UI.
    """

    date_joined_year = serializers.SerializerMethodField(
        help_text="Year the user joined; visible wherever the directory shows it."
    )

    # Serializer key -> visibility concept key, where they differ.
    # ``notify_channel`` is a private preference, grouped with ``language``.
    _CONCEPT_KEYS = {
        "date_joined": "date_joined_year",
        "date_joined_year": "date_joined_year",
        "notify_channel": "language",
    }

    @extend_schema_field(OpenApiTypes.INT)
    def get_date_joined_year(self, obj):
        return obj.date_joined.year if obj.date_joined else None

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "language",
            "notify_channel",
            "membership_level",
            "bio",
            "avatar",
            "qth_name",
            "qrz_url",
            "address",
            "phone",
            "latitude",
            "longitude",
            "locator",
            "is_directory_visible",
            "date_joined",
            "date_joined_year",
        ]
        extra_kwargs = {
            "date_joined": {
                "help_text": "Full join timestamp; only for Admins and the user themself."
            }
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        viewer = getattr(request, "user", None)
        if viewer is None:
            return {"id": data["id"]}
        visible = directory_visible_fields(viewer, instance)
        out = {
            key: value
            for key, value in data.items()
            if key == "id" or self._CONCEPT_KEYS.get(key, key) in visible
        }
        if audience_for(viewer, instance) == Audience.MEMBER:
            # Peers see the join year only (concept ``date_joined_year``).
            out.pop("date_joined", None)
        return out
