"""Explicit read-only serializers for the user/automation API (v1).

No ``fields="__all__"`` anywhere — sensitive fields are structurally absent.
"""

from rest_framework import serializers

from apps.control.models import StationModule
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
