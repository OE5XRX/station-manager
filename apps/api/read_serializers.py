"""Explicit read-only serializers for the user/automation API (v1).

No ``fields="__all__"`` anywhere — sensitive fields are structurally absent.
"""

from rest_framework import serializers

from apps.control.models import StationModule
from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageImportJob, ImageRelease
from apps.monitoring.models import Alert, AlertRule
from apps.provisioning.models import ProvisioningJob
from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
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
        fields = ["id", "sequence", "tag", "position"]


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
    progress = serializers.DictField(read_only=True)

    class Meta:
        model = Deployment
        fields = [
            "id",
            "image_release",
            "target_type",
            "target_tag",
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
