"""Explicit writable serializers for the user/automation API (v1).

Curated writable fields only — server-side actor fields and computed/
status/secret fields are read-only or absent. No ``fields="__all__"``.
"""

from rest_framework import serializers

from apps.deployments.models import Deployment
from apps.monitoring.models import AlertRule
from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationLogEntry,
    StationPhoto,
    StationTag,
)


class StationWriteSerializer(serializers.ModelSerializer):
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
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]


class RegionWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Region
        fields = ["id", "name", "slug", "description", "created_at"]
        read_only_fields = ["id", "created_at"]


class StationTagWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationTag
        fields = ["id", "name", "slug", "color", "description", "created_at"]
        read_only_fields = ["id", "created_at"]


class StationAssignmentWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationAssignment
        fields = ["id", "user", "station", "role", "assigned_at", "assigned_by"]
        read_only_fields = ["id", "assigned_at", "assigned_by"]


class RegionAssignmentWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = RegionAssignment
        fields = ["id", "user", "region", "role", "assigned_at", "assigned_by"]
        read_only_fields = ["id", "assigned_at", "assigned_by"]


class StationLogEntryWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationLogEntry
        fields = ["id", "station", "entry_type", "title", "message", "created_by", "created_at"]
        read_only_fields = ["id", "created_by", "created_at"]


class StationPhotoWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationPhoto
        fields = ["id", "station", "image", "caption", "uploaded_by", "uploaded_at"]
        read_only_fields = ["id", "uploaded_by", "uploaded_at"]


class RolloutSequenceWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = RolloutSequence
        fields = ["id", "singleton_key", "created_at", "updated_at", "updated_by"]
        read_only_fields = ["id", "created_at", "updated_at", "updated_by"]


class RolloutSequenceEntryWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = RolloutSequenceEntry
        fields = ["id", "sequence", "tag", "position"]
        read_only_fields = ["id"]


class AlertRuleWriteSerializer(serializers.ModelSerializer):
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
        read_only_fields = ["id", "created_at"]


class DeploymentCreateSerializer(serializers.ModelSerializer):
    """Create-only serializer for triggering a single-station deployment.

    Safe inputs only: image_release, target_station, strategy, phase_config.
    Everything else (target_type, status, created_by, target_tag) is server-set
    or absent — client values are silently discarded.
    """

    class Meta:
        model = Deployment
        fields = ["id", "image_release", "target_station", "strategy", "phase_config"]
        read_only_fields = ["id"]
