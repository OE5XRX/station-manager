"""Writable viewsets for the user/automation API (v1).

Each subclasses its Phase-2 read viewset (inheriting get_queryset scoping,
filters, nested read actions) and mixes in the DRF write mixins it needs.
Object-level write authz: TopologyScopedWritePermission -> can_write_object.
Create-scope: enforced in perform_create. Every mutation audits token origin.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.mixins import CreateModelMixin, DestroyModelMixin, UpdateModelMixin

from apps.accounts.models import AccountAuditLog
from apps.api import read_views
from apps.api import write_scoping as ws
from apps.api.audit import audit_account_write, audit_config_write, audit_station_write
from apps.api.write_permissions import TopologyScopedWritePermission
from apps.api.write_serializers import (
    RegionAssignmentWriteSerializer,
    RegionWriteSerializer,
    StationAssignmentWriteSerializer,
    StationTagWriteSerializer,
    StationWriteSerializer,
)
from apps.stations.models import StationAuditLog
from apps.stations.signals import discard_deleting_station


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
        audit_station_write(
            self.request,
            station=instance,
            event_type=StationAuditLog.EventType.DELETED,
            message=f"Station {instance.callsign or instance.name} deleted",
        )
        # The pre_delete signal adds this pk to a delete-tracking thread-local
        # that cascade sub-signals consult; the post_delete signal clears it.
        # If delete() raises, post_delete never fires — discard here so the
        # set can't leak into a later request on the same (pooled) thread.
        pk = instance.pk
        try:
            instance.delete()
        finally:
            discard_deleting_station(pk)


class RegionViewSet(
    ScopedWriteViewSet,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    read_views.RegionViewSet,
):
    write_serializer_class = RegionWriteSerializer

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


def _save_assignment(serializer, *, actor):
    """Call serializer.save(assigned_by=actor), converting any Django
    ValidationError raised by _ApplicantForbiddenMixin.full_clean() into a
    DRF ValidationError so the API returns 400 instead of 500.
    """
    try:
        return serializer.save(assigned_by=actor)
    except DjangoValidationError as exc:
        raise DRFValidationError(
            exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        )


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
        assignment = _save_assignment(serializer, actor=self.request.user)
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
        assignment = _save_assignment(serializer, actor=self.request.user)
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
        assignment = _save_assignment(serializer, actor=self.request.user)
        audit_account_write(
            self.request,
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED,
            target_user=assignment.user,
            region=assignment.region,
            message=f"RegionAssignment for {assignment.user} on {assignment.region} created",
        )

    def perform_update(self, serializer):
        self._guard_write()
        assignment = _save_assignment(serializer, actor=self.request.user)
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
