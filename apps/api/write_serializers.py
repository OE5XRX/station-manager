"""Explicit writable serializers for the user/automation API (v1).

Curated writable fields only — server-side actor fields and computed/
status/secret fields are read-only or absent. No ``fields="__all__"``.
"""

from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.accounts.models import User
from apps.deployments.models import Deployment
from apps.images.models import ImageRelease
from apps.monitoring.models import AlertRule
from apps.provisioning.models import ProvisioningJob
from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.stations.models import (
    RESERVED_TAG_SLUGS,
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationLogEntry,
    StationPhoto,
    StationTag,
)


class UserWriteSerializer(serializers.ModelSerializer):
    """Curated update-only serializer for User.

    Writable: profile fields + membership_level.
    Read-only: id, username, date_joined (stable identifiers) + email.
    Absent entirely (never writable): password, is_staff, is_superuser,
    is_active, last_login, deleted_at, deleted_by, groups, user_permissions.

    ``email`` is deliberately read-only: cross-user email mutation bypasses the
    self-service email-verification flow and enables account takeover (set the
    admin's email → hijack via password-reset). The UI never sets email
    cross-user; email changes go only through the verification flow. The field
    stays in ``fields`` so it still appears in responses.

    Validation mirrors UI guards (MembershipSetView = AdminRequiredMixin):
      - membership_level changes require an ADMIN actor (403).
      - self-change of membership_level is blocked (400).
      - demote-to-applicant blocked when user has assignments (400).
    The request is injected via context so the serializer can compare
    request.user with instance and check actor role.
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
        # email is read-only: cross-user email set = account-takeover vector.
        read_only_fields = ["id", "username", "date_joined", "email"]

    def validate(self, attrs):
        instance = self.instance
        if instance is None:
            return attrs  # create path — not used, but guard defensively

        new_level = attrs.get("membership_level")
        if new_level is None or new_level == instance.membership_level:
            return attrs

        request = self.context.get("request")

        # Actor-must-be-admin guard for ANY membership change (mirrors the UI's
        # AdminRequiredMixin on MembershipSetView). A staff (is_internal but not
        # is_admin) user may edit profile fields but may NOT change membership.
        # 403 (PermissionDenied), not 400 — this is an authorization failure.
        if request is not None and not request.user.is_admin:
            raise PermissionDenied("Only admins may change membership level.")

        # Self-change guard (mirrors MembershipSetView: target.pk == request.user.pk → 400).
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

    def validate_slug(self, value):
        # DRF ModelSerializer does NOT call model.clean(), so the reserved-slug
        # guard from StationTag.clean() would be bypassed and reach the DB
        # CheckConstraint → IntegrityError/500.  Mirror the model check here
        # for a proper 400 field error.
        if value in RESERVED_TAG_SLUGS:
            raise serializers.ValidationError("This slug is reserved by the rollout system.")
        return value


class StationAssignmentWriteSerializer(serializers.ModelSerializer):
    # B4: restrict to active (non-soft-deleted) users so that a PK of a
    # soft-deleted user returns a 400 "does-not-exist in queryset" error
    # rather than silently creating latent authorization.  Mirrors the UI
    # guard in apps/accounts/views_station_assignments.py (deleted_at__isnull).
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(deleted_at__isnull=True)
    )

    class Meta:
        model = StationAssignment
        fields = ["id", "user", "station", "role", "assigned_at", "assigned_by"]
        read_only_fields = ["id", "assigned_at", "assigned_by"]

    def validate(self, attrs):
        # B2: serializer-level guard for the one-admin-per-station constraint
        # (uniq_admin_per_station is a partial UniqueConstraint that DRF's
        # auto-generated UniqueTogetherValidator does not cover).  This gives
        # a 400 for the common case; _save_assignment catches the residual
        # IntegrityError race and returns 409.
        role = attrs.get("role")
        station = attrs.get("station")
        if role == StationAssignment.Role.ADMIN and station is not None:
            existing_qs = StationAssignment.objects.filter(
                station=station, role=StationAssignment.Role.ADMIN
            )
            # On update (partial_update), exclude the current instance.
            if self.instance is not None:
                existing_qs = existing_qs.exclude(pk=self.instance.pk)
            if existing_qs.exists():
                raise serializers.ValidationError(
                    {"role": "This station already has an admin assignment."}
                )
        return attrs


class RegionAssignmentWriteSerializer(serializers.ModelSerializer):
    # B4: restrict to active (non-soft-deleted) users so that a PK of a
    # soft-deleted user returns a 400 "does-not-exist in queryset" error.
    # Mirrors apps/accounts/views_region_assignments.py (deleted_at__isnull).
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(deleted_at__isnull=True)
    )

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
    """Write serializer for RolloutSequenceEntry.

    ``position`` is accepted from the client on PATCH (reorder) so the desired
    target position can be expressed, but the actual assignment is handled by
    the locked service (apps.rollouts.services) which does an atomic two-phase
    renumber.  The ``uniq_position_per_sequence`` UniqueTogetherValidator is
    therefore suppressed here: the service guarantees gap-free, conflict-free
    positions via select_for_update + two-phase move — pre-validating at the
    serializer level would wrongly reject valid reorder/create requests (the
    transient collision is exactly what the service resolves).

    ``sequence`` is create-only (read-only on update).  RolloutSequence is a
    DB singleton so reparenting an entry to "another" sequence is meaningless,
    and allowing it on PATCH would bypass the locked renumber on the original
    sequence.  Any ``sequence`` value in a PATCH body is silently ignored.

    ``tag`` uniqueness per sequence is enforced by a custom ``validate_tag``
    check: a duplicate tag returns a descriptive 400.  The auto-generated
    UniqueTogetherValidator is suppressed (``validators = []``) to prevent it
    from also rejecting valid reorder operations on the position constraint;
    we re-add only the tag check manually.
    """

    def validate_tag(self, value):
        """Reject a duplicate tag within the same sequence (400, not a DB 500)."""
        # On update (partial_update), instance is set — compare against the
        # sequence that already owns this entry (sequence is read-only on update).
        if self.instance is not None:
            sequence = self.instance.sequence
            qs = RolloutSequenceEntry.objects.filter(sequence=sequence, tag=value).exclude(
                pk=self.instance.pk
            )
        else:
            # On create, sequence comes from the incoming data.
            sequence = self.initial_data.get("sequence") or (
                self.validated_data.get("sequence") if hasattr(self, "_validated_data") else None
            )
            if sequence is None:
                # Can't validate without sequence; rely on service / DB constraint.
                return value
            qs = RolloutSequenceEntry.objects.filter(sequence=sequence, tag=value)
        if qs.exists():
            raise serializers.ValidationError("This tag is already in the sequence.")
        return value

    class Meta:
        model = RolloutSequenceEntry
        fields = ["id", "sequence", "tag", "position"]
        read_only_fields = ["id"]
        # Suppress auto-generated UniqueTogetherValidator for the position
        # constraint only. DRF generates one validator per UniqueConstraint;
        # we must explicitly empty the list and re-add the tag validator so
        # only the harmless one (tag-uniqueness for 400-on-duplicate) fires.
        # The position constraint is enforced atomically by the service layer.
        validators = []

    def to_internal_value(self, data):
        """Make ``sequence`` read-only on update: strip it from incoming data."""
        if self.instance is not None and "sequence" in data:
            data = {k: v for k, v in data.items() if k != "sequence"}
        return super().to_internal_value(data)


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


class ImageImportInputSerializer(serializers.Serializer):
    """Input serializer for the import/ action on ImageReleaseViewSet.

    The ``tag`` field is validated against the live GitHub releases list
    in the view (after deserialisation) so validation errors return 400
    with a field-specific error on ``tag``. The machine choices mirror
    ``ImageRelease.Machine``; channel defaults to "release".
    """

    tag = serializers.CharField(max_length=64)
    machine = serializers.ChoiceField(choices=ImageRelease.Machine.choices)
    channel = serializers.CharField(max_length=32, default="release")
    mark_as_latest = serializers.BooleanField(default=False)
