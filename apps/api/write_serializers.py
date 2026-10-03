"""Explicit writable serializers for the user/automation API (v1).

Curated writable fields only — server-side actor fields and computed/
status/secret fields are read-only or absent. No ``fields="__all__"``.
"""

from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.accounts.models import User
from apps.deployments.models import Deployment
from apps.monitoring.models import AlertRule
from apps.provisioning.models import ProvisioningJob
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

# Ordered membership levels for promote/demote direction determination.
# Mirrors MEMBERSHIP_ORDER in apps/accounts/views_membership.py.
_MEMBERSHIP_ORDER = [
    User.MembershipLevel.APPLICANT,
    User.MembershipLevel.MEMBER,
    User.MembershipLevel.STAFF,
    User.MembershipLevel.ADMIN,
]


class UserWriteSerializer(serializers.ModelSerializer):
    """Curated update-only serializer for User.

    Writable: profile fields + membership_level.
    Read-only: id, username, date_joined (stable identifiers).
    Absent entirely (never writable): password, is_staff, is_superuser,
    is_active, last_login, deleted_at, deleted_by, groups, user_permissions.

    Validation mirrors UI guards from MembershipSetView:
      - self-change of membership_level is blocked (400).
      - demote-to-applicant blocked when user has assignments (400).
    The request is injected via context so the serializer can compare
    request.user with instance for the self-change guard.
    """

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "date_joined",
            "first_name",
            "last_name",
            "email",
            "membership_level",
            "language",
            "notify_channel",
            "is_directory_visible",
            "bio",
            "qth_name",
            "qrz_url",
            "address",
            "phone",
            "latitude",
            "longitude",
            "locator",
        ]
        read_only_fields = ["id", "username", "date_joined"]

    def validate(self, attrs):
        instance = self.instance
        if instance is None:
            return attrs  # create path — not used, but guard defensively

        new_level = attrs.get("membership_level")
        if new_level is None or new_level == instance.membership_level:
            return attrs

        # Self-change guard (mirrors MembershipSetView: target.pk == request.user.pk → 400).
        request = self.context.get("request")
        if request is not None and request.user.pk == instance.pk:
            raise DRFValidationError(
                {"membership_level": "Cannot change your own membership level."}
            )

        # Demote-to-applicant guard when assignments exist
        # (mirrors MembershipSetView assignment-count check → 400).
        if new_level == User.MembershipLevel.APPLICANT:
            n_station = instance.station_assignments.count()
            n_region = instance.region_assignments.count()
            if n_station or n_region:
                raise DRFValidationError(
                    {
                        "membership_level": (
                            f"Cannot demote to Applicant: user has {n_station} station "
                            f"and {n_region} region assignment(s). Remove them first."
                        )
                    }
                )

        return attrs


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


class ProvisioningJobCreateSerializer(serializers.ModelSerializer):
    """Create-only serializer for triggering a ProvisioningJob.

    Safe inputs: station, image_release.
    Server-set (absent from writable fields): requested_by, status, timestamps.
    """

    class Meta:
        model = ProvisioningJob
        fields = ["id", "station", "image_release"]
        read_only_fields = ["id"]
