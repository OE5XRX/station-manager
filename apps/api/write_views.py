"""Writable viewsets for the user/automation API (v1).

Each subclasses its Phase-2 read viewset (inheriting get_queryset scoping,
filters, nested read actions) and mixes in the DRF write mixins it needs.
Object-level write authz: TopologyScopedWritePermission -> can_write_object.
Create-scope: enforced in perform_create. Every mutation audits token origin.
"""

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as rf_serializers
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.mixins import CreateModelMixin, DestroyModelMixin, UpdateModelMixin
from rest_framework.response import Response

from apps.accounts.models import AccountAuditLog, User
from apps.api import read_serializers, read_views
from apps.api import write_scoping as ws
from apps.api.audit import audit_account_write, audit_config_write, audit_station_write
from apps.api.write_permissions import TopologyScopedWritePermission
from apps.api.write_serializers import (
    AlertRuleWriteSerializer,
    DeploymentCreateSerializer,
    ImageImportInputSerializer,
    ProvisioningJobCreateSerializer,
    RegionAssignmentWriteSerializer,
    RegionWriteSerializer,
    RolloutSequenceEntryWriteSerializer,
    RolloutSequenceWriteSerializer,
    StationAssignmentWriteSerializer,
    StationLogEntryWriteSerializer,
    StationPhotoWriteSerializer,
    StationTagWriteSerializer,
    StationWriteSerializer,
    UserWriteSerializer,
)
from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.supersession import (
    ActiveDeploymentConflictError,
    supersede_pending_for_station,
)
from apps.images import github_releases
from apps.images.models import ImageImportJob, ImageRelease
from apps.provisioning.models import ProvisioningJob
from apps.provisioning.views import ACTIVE_PROVISIONING_STATUSES
from apps.stations.models import StationAuditLog, StationLogEntry, StationPhoto


class ScopedWriteViewSet:
    """Mixin adding write-serializer switching + object-level write perm.

    Place BEFORE the read viewset in the MRO. Concrete viewsets add the
    CreateModelMixin/UpdateModelMixin/DestroyModelMixin they actually expose
    and override can_write_object / perform_* as needed.
    """

    permission_classes = [TopologyScopedWritePermission]
    write_serializer_class = None

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update") and self.write_serializer_class:
            return self.write_serializer_class
        return super().get_serializer_class()

    def can_write_object(self, user, obj, method):
        return False


class StationViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.StationViewSet,
):
    write_serializer_class = StationWriteSerializer

    def can_write_object(self, user, obj, method):
        if method == "DELETE":
            return ws.can_delete_station(user, obj)
        return ws.can_write_station(user, obj)

    def perform_create(self, serializer):
        region = serializer.validated_data.get("region")
        if region is None or not ws.can_create_station_in(self.request.user, region):
            raise PermissionDenied("Not allowed to create a station in this region.")
        station = serializer.save()
        audit_station_write(
            self.request,
            station=station,
            event_type=StationAuditLog.EventType.CREATED,
            message=f"Station {station.callsign or station.name} created",
        )

    def perform_update(self, serializer):
        # `region` is writable: re-validate the NEW target region before saving,
        # else a region-manager could PATCH a station they manage into a region
        # they don't (move-via-update privilege escalation). can_write_object
        # only checked the OLD region.
        new_region = serializer.validated_data.get("region", serializer.instance.region)
        if not ws.can_create_station_in(self.request.user, new_region):
            raise PermissionDenied("Not allowed to move this station to that region.")
        station = serializer.save()
        audit_station_write(
            self.request,
            station=station,
            event_type=StationAuditLog.EventType.UPDATED,
            message=f"Station {station.callsign or station.name} updated",
        )

    def perform_destroy(self, instance):
        # Both the audit write and the delete run inside a single atomic
        # savepoint so that:
        #   - a ProtectedError from instance.delete() rolls back the audit
        #     row too (no false "deleted" record for a station that still
        #     exists), and
        #   - a transient audit failure rolls back the delete (keeps the
        #     station and audit trail consistent).
        # The audit is written BEFORE delete() so StationAuditLog.station
        # still references a live row — the SET_NULL cascade then nulls it
        # after the station row is removed, which is the intended behaviour.
        label = instance.callsign or instance.name
        try:
            with transaction.atomic():
                audit_station_write(
                    self.request,
                    station=instance,
                    event_type=StationAuditLog.EventType.DELETED,
                    message=f"Station {label} deleted",
                )
                instance.delete()
        except ProtectedError as exc:
            # One or more PROTECT FKs (e.g. DeploymentResult.station) block
            # deletion.  Return 409 Conflict so the caller knows the station
            # still exists and why.
            protected_models = ", ".join(
                sorted({obj.__class__.__name__ for obj in exc.protected_objects})
            )
            err = APIException(
                detail=(f"Station cannot be deleted while referenced by: {protected_models}.")
            )
            err.status_code = http_status.HTTP_409_CONFLICT
            raise err from exc


class RegionViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.RegionViewSet,
):
    write_serializer_class = RegionWriteSerializer
    # B5: payload-independent create gate — role check runs in has_permission
    # before DRF validates the serializer (prevents non-staff probing names).
    create_requires = staticmethod(ws.can_write_region)

    def can_write_object(self, user, obj, method):
        return ws.can_write_region(user)

    def _guard_create(self):
        if not ws.can_write_region(self.request.user):
            raise PermissionDenied("Region writes require staff/admin.")

    def perform_create(self, serializer):
        self._guard_create()
        region = serializer.save()
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.REGION_CREATED,
            region=region,
            message=f"Region {region.slug} created",
        )

    def perform_update(self, serializer):
        region = serializer.save()
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.REGION_UPDATED,
            region=region,
            message=f"Region {region.slug} updated",
        )

    def perform_destroy(self, instance):
        # Pass region=instance while the row still exists; AccountAuditLog.region
        # is SET_NULL, so the FK is captured before the cascade nulls it.
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.REGION_DELETED,
            region=instance,
            message=f"Region {instance.slug} deleted",
        )
        instance.delete()


class StationTagViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.StationTagViewSet,
):
    write_serializer_class = StationTagWriteSerializer
    # B5: payload-independent create gate — role check runs in has_permission
    # before DRF validates the serializer (prevents non-staff probing slugs).
    create_requires = staticmethod(ws.can_write_station_tag)

    def can_write_object(self, user, obj, method):
        return ws.can_write_station_tag(user)

    def _guard_create(self):
        if not ws.can_write_station_tag(self.request.user):
            raise PermissionDenied("StationTag writes require staff/admin.")

    def perform_create(self, serializer):
        self._guard_create()
        tag = serializer.save()
        audit_config_write(
            self.request,
            message=f"StationTag {tag.slug} created",
        )

    def perform_update(self, serializer):
        tag = serializer.save()
        audit_config_write(
            self.request,
            message=f"StationTag {tag.slug} updated",
        )

    def perform_destroy(self, instance):
        audit_config_write(
            self.request,
            message=f"StationTag {instance.slug} deleted",
        )
        instance.delete()


def _save_assignment(serializer, *, actor, is_create):
    """Save an assignment serializer, handling validation and integrity errors.

    B3: inject assigned_by=actor ONLY on create so that a PATCH by a different
    editor does not overwrite the original assignment provenance.

    B2 (race): catch IntegrityError from residual DB-level unique-constraint
    violations (e.g. two concurrent creates that both pass serializer
    validation) and translate to 409 Conflict instead of 500.
    """
    save_kwargs = {}
    if is_create:
        save_kwargs["assigned_by"] = actor
    try:
        return serializer.save(**save_kwargs)
    except DjangoValidationError as exc:
        raise DRFValidationError(
            exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        )
    except IntegrityError as exc:
        # Residual race: two concurrent creates both passed serializer
        # validation but one hit the DB unique constraint first.
        err = APIException(detail="Assignment already exists or conflicts with an existing one.")
        err.status_code = http_status.HTTP_409_CONFLICT
        raise err from exc


class StationAssignmentViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.StationAssignmentViewSet,
):
    write_serializer_class = StationAssignmentWriteSerializer

    def can_write_object(self, user, obj, method):
        return ws.can_write_station_assignment(user, obj.station)

    def perform_create(self, serializer):
        station = serializer.validated_data["station"]
        if not ws.can_write_station_assignment(self.request.user, station):
            raise PermissionDenied("Not allowed to create an assignment for this station.")
        assignment = _save_assignment(serializer, actor=self.request.user, is_create=True)
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            target_user=assignment.user,
            message=f"StationAssignment for {assignment.user} on {assignment.station} created",
        )

    def perform_update(self, serializer):
        # `station` is writable: re-validate the NEW target station before saving,
        # else a region-manager could PATCH an in-scope assignment to move it onto
        # an out-of-scope station (move-via-update privilege escalation).
        # can_write_object only checked the OLD station.
        new_station = serializer.validated_data.get("station", serializer.instance.station)
        if not ws.can_write_station_assignment(self.request.user, new_station):
            raise PermissionDenied("Not allowed to move this assignment to that station.")
        # B3: is_create=False → assigned_by is NOT overwritten (provenance preserved)
        assignment = _save_assignment(serializer, actor=self.request.user, is_create=False)
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_UPDATED,
            target_user=assignment.user,
            message=f"StationAssignment for {assignment.user} on {assignment.station} updated",
        )

    def perform_destroy(self, instance):
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            target_user=instance.user,
            message=f"StationAssignment for {instance.user} on {instance.station} deleted",
        )
        instance.delete()


class RegionAssignmentViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.RegionAssignmentViewSet,
):
    write_serializer_class = RegionAssignmentWriteSerializer

    def can_write_object(self, user, obj, method):
        return ws.can_write_region_assignment(user)

    def _guard_write(self):
        if not ws.can_write_region_assignment(self.request.user):
            raise PermissionDenied("Region assignment writes require staff/admin.")

    def perform_create(self, serializer):
        self._guard_write()
        assignment = _save_assignment(serializer, actor=self.request.user, is_create=True)
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED,
            target_user=assignment.user,
            region=assignment.region,
            message=f"RegionAssignment for {assignment.user} on {assignment.region} created",
        )

    def perform_update(self, serializer):
        self._guard_write()
        # B3: is_create=False → assigned_by is NOT overwritten (provenance preserved)
        assignment = _save_assignment(serializer, actor=self.request.user, is_create=False)
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_UPDATED,
            target_user=assignment.user,
            region=assignment.region,
            message=f"RegionAssignment for {assignment.user} on {assignment.region} updated",
        )

    def perform_destroy(self, instance):
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
            target_user=instance.user,
            region=instance.region,
            message=f"RegionAssignment for {instance.user} on {instance.region} deleted",
        )
        instance.delete()


class StationLogEntryViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.ScopedReadOnlyViewSet,
):
    """CRUD for StationLogEntry, scoped to the user's accessible stations."""

    serializer_class = StationLogEntryWriteSerializer
    write_serializer_class = StationLogEntryWriteSerializer

    def get_queryset(self):
        return StationLogEntry.objects.filter(
            station__in=ws.accessible_stations(self.request.user)
        ).order_by("-created_at")

    def can_write_object(self, user, obj, method):
        return ws.can_write_station_content(user, obj.station)

    def perform_create(self, serializer):
        station = serializer.validated_data["station"]
        if not ws.can_write_station_content(self.request.user, station):
            raise PermissionDenied("Not allowed to create a log entry for this station.")
        entry = serializer.save(created_by=self.request.user)
        audit_station_write(
            self.request,
            station=entry.station,
            event_type=StationAuditLog.EventType.CREATED,
            message=f"StationLogEntry '{entry.title}' created on {entry.station}",
        )

    def perform_update(self, serializer):
        # `station` is writable: re-validate the NEW target station before saving,
        # else a station-user could move a log entry to an out-of-scope station.
        new_station = serializer.validated_data.get("station", serializer.instance.station)
        if not ws.can_write_station_content(self.request.user, new_station):
            raise PermissionDenied("Not allowed to move this log entry to that station.")
        entry = serializer.save()
        audit_station_write(
            self.request,
            station=entry.station,
            event_type=StationAuditLog.EventType.UPDATED,
            message=f"StationLogEntry '{entry.title}' updated on {entry.station}",
        )

    def perform_destroy(self, instance):
        audit_station_write(
            self.request,
            station=instance.station,
            event_type=StationAuditLog.EventType.DELETED,
            message=f"StationLogEntry '{instance.title}' deleted on {instance.station}",
        )
        instance.delete()


class StationPhotoViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.ScopedReadOnlyViewSet,
):
    """CRUD for StationPhoto, scoped to the user's accessible stations."""

    serializer_class = StationPhotoWriteSerializer
    write_serializer_class = StationPhotoWriteSerializer

    def get_queryset(self):
        return StationPhoto.objects.filter(
            station__in=ws.accessible_stations(self.request.user)
        ).order_by("-uploaded_at")

    def can_write_object(self, user, obj, method):
        return ws.can_write_station_content(user, obj.station)

    def perform_create(self, serializer):
        station = serializer.validated_data["station"]
        if not ws.can_write_station_content(self.request.user, station):
            raise PermissionDenied("Not allowed to upload a photo for this station.")
        photo = serializer.save(uploaded_by=self.request.user)
        audit_station_write(
            self.request,
            station=photo.station,
            event_type=StationAuditLog.EventType.CREATED,
            message=f"StationPhoto uploaded on {photo.station}",
        )

    def perform_update(self, serializer):
        # `station` is writable: re-validate the NEW target station before saving,
        # else a station-user could move a photo to an out-of-scope station.
        new_station = serializer.validated_data.get("station", serializer.instance.station)
        if not ws.can_write_station_content(self.request.user, new_station):
            raise PermissionDenied("Not allowed to move this photo to that station.")
        photo = serializer.save()
        audit_station_write(
            self.request,
            station=photo.station,
            event_type=StationAuditLog.EventType.UPDATED,
            message=f"StationPhoto updated on {photo.station}",
        )

    def perform_destroy(self, instance):
        audit_station_write(
            self.request,
            station=instance.station,
            event_type=StationAuditLog.EventType.DELETED,
            message=f"StationPhoto deleted on {instance.station}",
        )
        instance.delete()


class RolloutSequenceViewSet(
    ScopedWriteViewSet,
    UpdateModelMixin,
    read_views.RolloutSequenceViewSet,
):
    """Update-only for the singleton RolloutSequence (global, region-mgr/staff)."""

    write_serializer_class = RolloutSequenceWriteSerializer

    def can_write_object(self, user, obj, method):
        return ws.can_write_rollouts(user)

    def perform_update(self, serializer):
        seq = serializer.save(updated_by=self.request.user)
        audit_config_write(
            self.request,
            message=f"RolloutSequence {seq.pk} updated",
        )


class RolloutSequenceEntryViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.RolloutSequenceEntryViewSet,
):
    """Full CRUD for RolloutSequenceEntry (global, region-mgr/staff)."""

    write_serializer_class = RolloutSequenceEntryWriteSerializer
    # B5: payload-independent create gate (role only, no target FK involved).
    create_requires = staticmethod(ws.can_write_rollouts)

    def can_write_object(self, user, obj, method):
        return ws.can_write_rollouts(user)

    def _guard_create(self):
        if not ws.can_write_rollouts(self.request.user):
            raise PermissionDenied("RolloutSequenceEntry writes require region-manager or staff.")

    def perform_create(self, serializer):
        self._guard_create()
        entry = serializer.save()
        audit_config_write(
            self.request,
            message=f"RolloutSequenceEntry {entry.pk} created",
        )

    def perform_update(self, serializer):
        entry = serializer.save()
        audit_config_write(
            self.request,
            message=f"RolloutSequenceEntry {entry.pk} updated",
        )

    def perform_destroy(self, instance):
        audit_config_write(
            self.request,
            message=f"RolloutSequenceEntry {instance.pk} deleted",
        )
        instance.delete()


class AlertRuleViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.AlertRuleViewSet,
):
    """Full CRUD for AlertRule (global, region-mgr/staff).

    AlertRule is a global config resource with no topology FK — gated purely
    by role (is_any_region_manager). alert_type is UNIQUE per the model, so
    create will fail with 400 if the client POSTs a duplicate type.
    """

    write_serializer_class = AlertRuleWriteSerializer
    # B5: payload-independent create gate (role only, no target FK).
    create_requires = staticmethod(ws.can_write_alert_rule)

    def can_write_object(self, user, obj, method):
        return ws.can_write_alert_rule(user)

    def _guard_create(self):
        if not ws.can_write_alert_rule(self.request.user):
            raise PermissionDenied("AlertRule writes require region-manager or staff.")

    def perform_create(self, serializer):
        self._guard_create()
        rule = serializer.save()
        audit_config_write(
            self.request,
            message=f"AlertRule {rule.alert_type} created",
        )

    def perform_update(self, serializer):
        rule = serializer.save()
        audit_config_write(
            self.request,
            message=f"AlertRule {rule.alert_type} updated",
        )

    def perform_destroy(self, instance):
        audit_config_write(
            self.request,
            message=f"AlertRule {instance.alert_type} deleted",
        )
        instance.delete()


class DeploymentViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    read_views.DeploymentViewSet,
):
    """Create-only (trigger) endpoint for Deployments.

    POST  /deployments/ — fire a new STATION-targeted deploy.
    GET   /deployments/ — inherited list (read-only).
    GET   /deployments/{pk}/ — inherited retrieve (read-only).
    PATCH/PUT/DELETE — not mixed in → 405.

    Authz: internal OR region-manager of the target station's region.
    target_type is always forced to STATION; status/created_by/target_type
    from the client body are ignored (server-set).
    """

    write_serializer_class = DeploymentCreateSerializer

    def can_write_object(self, user, obj, method):
        # No object-level writes (no Update/Destroy mixin).
        # Belt-and-suspenders: deny everything so an accidental UpdateMixin
        # addition cannot sneak through.
        return False

    def perform_create(self, serializer):
        station = serializer.validated_data["target_station"]
        # Authz first: an unauthorized user gets 403 regardless of image
        # validity (403-before-400 ordering).
        if not ws.can_trigger_deployment(self.request.user, station):
            raise PermissionDenied("Not allowed to trigger a deployment for this station.")

        image_release = serializer.validated_data["image_release"]

        # Input-validation parity with the canonical UI trigger
        # (apps.rollouts.views.UpgradeStationView): field-specific 400s BEFORE
        # any DB write, so a wrong-architecture or non-deployable image can't be
        # queued.
        current = station.current_image_release
        if current is None:
            raise DRFValidationError(
                {
                    "target_station": (
                        "Station has no current image release; cannot target a deployment."
                    )
                }
            )
        if image_release.machine != current.machine:
            raise DRFValidationError(
                {
                    "image_release": (
                        f"Image machine {image_release.machine} does not match "
                        f"station machine {current.machine}."
                    )
                }
            )
        if not image_release.is_ota_ready:
            raise DRFValidationError(
                {"image_release": f"Image release {image_release.tag} is not OTA-ready."}
            )

        strategy = serializer.validated_data.get("strategy", Deployment.Strategy.IMMEDIATE)
        phase_config = serializer.validated_data.get("phase_config", {})

        try:
            with transaction.atomic():
                dep = Deployment.objects.create(
                    image_release=image_release,
                    target_type=Deployment.TargetType.STATION,
                    target_station=station,
                    status=Deployment.Status.IN_PROGRESS,
                    created_by=self.request.user,
                    strategy=strategy,
                    phase_config=phase_config,
                )
                DeploymentResult.objects.create(
                    deployment=dep,
                    station=station,
                    status=DeploymentResult.Status.PENDING,
                    previous_version=station.current_os_version or "",
                )
                supersede_pending_for_station(station=station, new_deployment=dep)
        except ActiveDeploymentConflictError as exc:
            raise DRFValidationError(str(exc))

        audit_station_write(
            self.request,
            station=station,
            event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
            message=f"Deployment #{dep.id} triggered via API for station {station}",
        )
        # Set the serializer instance so DRF returns the created object in the
        # 201 response (CreateModelMixin.create() calls serializer.save() which
        # we bypassed — set instance directly so get_success_headers works too).
        serializer.instance = dep


class ProvisioningJobViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    read_views.ProvisioningJobViewSet,
):
    """Create-only (trigger) endpoint for ProvisioningJobs.

    POST  /provisioning-jobs/ — fire a new provisioning job for a station.
    GET   /provisioning-jobs/ — inherited list (read-only).
    GET   /provisioning-jobs/{pk}/ — inherited retrieve (read-only).
    PATCH/PUT/DELETE — not mixed in → 405.

    Authz: staff/admin only (is_internal).  requested_by and status are
    always server-set; any client-supplied values are ignored.
    """

    write_serializer_class = ProvisioningJobCreateSerializer

    def can_write_object(self, user, obj, method):
        # No object-level writes (no Update/Destroy mixin).
        return False

    def perform_create(self, serializer):
        station = serializer.validated_data["station"]

        # Authz first: 403 before any input-validation 400 (403-before-400 ordering).
        if not ws.can_trigger_provisioning(self.request.user, station):
            raise PermissionDenied("Not allowed to trigger a provisioning job for this station.")

        # Input-validation parity with the UI trigger (CreateProvisioningJobView):
        # reject if an active job (PENDING/RUNNING/READY) already exists for the
        # station — prevents duplicate provisioning bundles being queued.
        if ProvisioningJob.objects.filter(
            station=station, status__in=ACTIVE_PROVISIONING_STATUSES
        ).exists():
            raise DRFValidationError(
                {"station": "This station already has an active provisioning job."}
            )

        job = serializer.save(requested_by=self.request.user)
        audit_station_write(
            self.request,
            station=station,
            event_type=StationAuditLog.EventType.PROVISIONING_REQUESTED,
            message=f"ProvisioningJob #{job.id} requested via API for station {station}",
        )
        # Set instance so DRF returns 201 with the created object (mirrors
        # DeploymentViewSet — we bypassed serializer.save()'s default path).
        serializer.instance = job


# Sequential ordering of membership levels for promote/demote direction.
# Mirrors MEMBERSHIP_ORDER in apps/accounts/views_membership.py.
_USER_MEMBERSHIP_ORDER = [
    User.MembershipLevel.APPLICANT,
    User.MembershipLevel.MEMBER,
    User.MembershipLevel.STAFF,
    User.MembershipLevel.ADMIN,
]


class UserViewSet(
    ScopedWriteViewSet,
    UpdateModelMixin,
    read_views.UserViewSet,
):
    """Update-only endpoint for User profiles and membership level.

    POST (create) → 405  — user creation via API is out of scope for Phase 3.
    DELETE (destroy) → 405 — user deletion is a soft/hard-purge admin flow.
    PATCH/PUT → staff/admin only (is_internal); see level-aware target rules.

    get_queryset is overridden to exclude soft-deleted users (mirrors the UI's
    ``deleted_at__isnull=True``): a PATCH on a soft-deleted user → 404. Internal
    users see all active users; non-internal see only themselves (and cannot
    write anyway — can_write_object gates writes).

    Authorization model (mirrors the UI):
      - Write access requires is_internal (staff/admin).
      - A non-admin (staff) may edit ONLY non-internal targets
        (members/applicants) and may NOT change membership_level at all.
      - An admin may edit anyone including membership_level.
      - email is read-only (account-takeover vector) — serializer-enforced.

    Audit events:
      - membership_level changed upward  → MEMBERSHIP_PROMOTED
      - membership_level changed downward → MEMBERSHIP_DEMOTED
      - any other field change only       → USER_UPDATED
    All messages include "via API token <prefix>" via audit_account_write.

    UI guards mirrored from MembershipSetView (apps/accounts/views_membership.py):
      1. membership_level changes require an admin actor → 403 (serializer).
      2. Self-change of membership_level → 400 (validated in serializer).
      3. Demote-to-applicant when assignments exist → 400 (validated in serializer).
    """

    write_serializer_class = UserWriteSerializer

    def get_queryset(self):
        # Mirror the UI: soft-deleted users are off-limits to mutation. Narrow
        # the inherited scope to the active slice so PATCH on a soft-deleted
        # user → 404 (invisible), never a mutation on a dead row.
        return super().get_queryset().filter(deleted_at__isnull=True)

    def can_write_object(self, user, obj, method):
        if not ws.can_write_user(user):
            return False
        # Level-aware target protection: a non-admin (staff) must not mutate a
        # staff/admin target. Only admins may touch internal users. This blocks
        # a staff user from editing peers' or admins' profiles entirely.
        if not user.is_admin and obj.is_internal:
            return False
        return True

    def perform_update(self, serializer):
        old_level = serializer.instance.membership_level
        obj = serializer.save()
        new_level = obj.membership_level

        if new_level != old_level:
            # Bust cached_property so is_internal/is_admin reflect the new level
            # within this request (e.g. for any post-save permission checks).
            User._invalidate_role_cache(obj)
            old_idx = _USER_MEMBERSHIP_ORDER.index(User.MembershipLevel(old_level))
            new_idx = _USER_MEMBERSHIP_ORDER.index(User.MembershipLevel(new_level))
            event_type = (
                AccountAuditLog.EventType.MEMBERSHIP_PROMOTED
                if new_idx > old_idx
                else AccountAuditLog.EventType.MEMBERSHIP_DEMOTED
            )
        else:
            event_type = AccountAuditLog.EventType.USER_UPDATED

        audit_account_write(
            self.request,
            event_type=event_type,
            target_user=obj,
            message=f"User {obj.username} updated",
        )


_LINUX_IMAGE_REPO_DEFAULT = "OE5XRX/linux-image"
_GITHUB_RELEASES_LIMIT = 30


class ImageReleaseViewSet(ScopedWriteViewSet, read_views.ImageReleaseViewSet):
    """ImageRelease special write surface: archive/restore + available + import.

    No generic create/update/destroy (no write mixins added). Hard DELETE
    on images/{id}/ → 405 (inherited from ReadOnlyModelViewSet; no
    DestroyModelMixin here). Every mutation is staff-only (can_manage_images).

    Actions:
      POST images/{id}/archive/  — soft-delete (idempotent)
      POST images/{id}/restore/  — undo archive (idempotent)
      GET  images/available/     — proxy GitHub releases list
      POST images/import/        — queue an ImageImportJob
    """

    def can_write_object(self, user, obj, method):
        # No generic object writes (no Update/Destroy mixin). All mutations
        # go through custom actions that gate themselves via can_manage_images.
        return False

    # ------------------------------------------------------------------ #
    # archive / restore                                                    #
    # ------------------------------------------------------------------ #

    @extend_schema(request=None, responses={200: read_serializers.ImageReleaseSerializer})
    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None):
        """Soft-delete a release. Idempotent. Staff-only."""
        if not ws.can_manage_images(request.user):
            raise PermissionDenied("Image management requires staff/admin.")
        obj = get_object_or_404(ImageRelease.all_objects, pk=pk)
        obj.archive()
        audit_config_write(request, message=f"ImageRelease {obj.tag} archived")
        ser = read_serializers.ImageReleaseSerializer(obj, context={"request": request})
        return Response(ser.data)

    @extend_schema(request=None, responses={200: read_serializers.ImageReleaseSerializer})
    @action(detail=True, methods=["post"], url_path="restore")
    def restore(self, request, pk=None):
        """Undo a previous archive. Idempotent. Staff-only."""
        if not ws.can_manage_images(request.user):
            raise PermissionDenied("Image management requires staff/admin.")
        obj = get_object_or_404(ImageRelease.all_objects, pk=pk)
        obj.restore()
        audit_config_write(request, message=f"ImageRelease {obj.tag} restored")
        ser = read_serializers.ImageReleaseSerializer(obj, context={"request": request})
        return Response(ser.data)

    # ------------------------------------------------------------------ #
    # available — GitHub releases proxy                                   #
    # ------------------------------------------------------------------ #

    @extend_schema(
        responses={
            200: inline_serializer(
                "GitHubRelease",
                fields={
                    "tag": rf_serializers.CharField(),
                    "html_url": rf_serializers.URLField(),
                    "is_latest": rf_serializers.BooleanField(),
                    "asset_names": rf_serializers.ListField(child=rf_serializers.CharField()),
                },
                many=True,
            )
        }
    )
    @action(detail=False, methods=["get"], url_path="available")
    def available(self, request):
        """List available GitHub releases. Staff-only."""
        if not ws.can_manage_images(request.user):
            raise PermissionDenied("Image management requires staff/admin.")
        repo = getattr(settings, "LINUX_IMAGE_REPO", _LINUX_IMAGE_REPO_DEFAULT)
        releases = github_releases.fetch_releases(repo, limit=_GITHUB_RELEASES_LIMIT)
        data = [
            {
                "tag": r.tag,
                "html_url": r.html_url,
                "is_latest": r.is_latest,
                "asset_names": sorted(r.asset_names),
            }
            for r in releases
        ]
        return Response(data)

    # ------------------------------------------------------------------ #
    # import — queue an ImageImportJob                                    #
    # ------------------------------------------------------------------ #

    @extend_schema(
        request=ImageImportInputSerializer,
        responses={202: read_serializers.ImageImportJobSerializer},
    )
    @action(detail=False, methods=["post"], url_path="import", url_name="import")
    def import_release(self, request):
        """Queue an ImageImportJob for a given GitHub release tag. Staff-only.

        The ``tag`` must exist in the current GitHub releases list — free-text
        or unknown tags are rejected with a 400 field error on ``tag``.
        """
        if not ws.can_manage_images(request.user):
            raise PermissionDenied("Image management requires staff/admin.")

        serializer = ImageImportInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        tag = validated["tag"]
        machine = validated["machine"]
        channel = validated["channel"]
        mark_as_latest = validated["mark_as_latest"]

        # Validate tag against live GitHub releases.
        repo = getattr(settings, "LINUX_IMAGE_REPO", _LINUX_IMAGE_REPO_DEFAULT)
        releases = github_releases.fetch_releases(repo, limit=_GITHUB_RELEASES_LIMIT)
        known_tags = {r.tag for r in releases}
        if tag not in known_tags:
            raise DRFValidationError(
                {"tag": f"Tag '{tag}' not found in available GitHub releases."}
            )

        job = ImageImportJob.objects.create(
            tag=tag,
            machine=machine,
            channel=channel,
            mark_as_latest=mark_as_latest,
            requested_by=request.user,
        )
        audit_config_write(
            request,
            message=f"ImageRelease import {tag}/{machine} requested",
        )
        return Response(
            read_serializers.ImageImportJobSerializer(job, context={"request": request}).data,
            status=202,
        )
