import logging

from django.contrib.auth.decorators import login_not_required
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice
from apps.stations.models import StationAuditLog

from .models import Module, ModuleAssignmentHistory, ModuleFirmwareConvergenceState
from .reconcile_serializers import (
    ReconcileCheckRequestSerializer,
    ReconcileCheckResponseSerializer,
    ReconcileCommitSerializer,
    ReconcileStatusSerializer,
)
from .reconciler import _audit, reconcile_module, record_error

logger = logging.getLogger(__name__)


@method_decorator(login_not_required, name="dispatch")
class ReconcileCheckView(APIView):
    """Return exactly one firmware instruction for the calling station, or 204.

    Selection: among the station's open assignments (deterministic slot order),
    reconcile each module; prefer a mid-flight ``updating`` convergence (resume
    after an agent crash), otherwise the first drifted, non-quarantined module.
    """

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        req = ReconcileCheckRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)

        assignments = (
            ModuleAssignmentHistory.objects.filter(station=station, to_ts__isnull=True)
            .select_related("module", "module__module_type")
            .order_by("slot")
        )

        resume = None  # a mid-flight updating instruction
        first_drift = None  # first fresh-drift instruction

        for a in assignments:
            module = a.module
            pre_existing = ModuleFirmwareConvergenceState.objects.filter(
                module=module,
                state=ModuleFirmwareConvergenceState.State.UPDATING,
            ).exists()
            cs = reconcile_module(module)
            if cs is None or cs.state != ModuleFirmwareConvergenceState.State.UPDATING:
                continue
            instruction = self._instruction(module, a.slot, cs)
            if pre_existing:
                resume = resume or instruction
            else:
                first_drift = first_drift or instruction

        chosen = resume or first_drift
        if chosen is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(ReconcileCheckResponseSerializer(chosen).data)

    def _instruction(self, module, slot, cs):
        release = cs.target_release
        return {
            "convergence_id": cs.pk,
            "module_uid": module.uid,
            "slot": slot,
            "module_type": module.module_type.key,
            "variant": module.variant,
            "target_version": release.version,
            "download_url": reverse("module_firmware_api:download", args=[release.pk]),
            "checksum_sha256": release.sha256,
            "size_bytes": release.size_bytes,
        }


_STATUS_TO_ERROR = {
    "failed": ModuleFirmwareConvergenceState.ErrorMode.TRANSIENT,
    "rolled_back": ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK,
    "rejected": ModuleFirmwareConvergenceState.ErrorMode.REJECTED,
}
_STATUS_TO_AUDIT = {
    "failed": StationAuditLog.EventType.MODULE_FLASH_FAILED,
    "rolled_back": StationAuditLog.EventType.MODULE_FLASH_ROLLED_BACK,
    "rejected": StationAuditLog.EventType.MODULE_FLASH_REJECTED,
}


@method_decorator(login_not_required, name="dispatch")
class ReconcileStatusUpdateView(APIView):
    """Agent reports flash progress/outcome for one convergence row."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request, convergence_id):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ReconcileStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data["status"]
        error_message = serializer.validated_data.get("error_message", "")

        with transaction.atomic():
            try:
                cs = (
                    ModuleFirmwareConvergenceState.objects.select_for_update()
                    .select_related("module")
                    .get(pk=convergence_id)
                )
            except ModuleFirmwareConvergenceState.DoesNotExist:
                return Response(
                    {"detail": "Convergence not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Authz: the module must be currently assigned to THIS station.
            bound = cs.module.assignments.filter(station=station, to_ts__isnull=True).exists()
            if not bound:
                return Response(
                    {"detail": "Convergence not bound to this station."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
                return Response(
                    {"detail": "Convergence is quarantined; no updates accepted."},
                    status=status.HTTP_409_CONFLICT,
                )

            error_mode = _STATUS_TO_ERROR.get(new_status)
            if error_mode is not None:
                record_error(cs, error_mode, error_message=error_message)
                _audit(
                    cs.module,
                    _STATUS_TO_AUDIT[new_status],
                    f"Module {cs.module.uid} flash {new_status} "
                    f"(target {cs.target_release.version}).",
                    station=station,
                )
            else:
                # progress (downloading/flashing/verifying)
                if cs.last_attempt_at is None:
                    _audit(
                        cs.module,
                        StationAuditLog.EventType.MODULE_FLASH_STARTED,
                        f"Module {cs.module.uid} flash started "
                        f"(target {cs.target_release.version}).",
                        station=station,
                    )
                    cs.last_attempt_at = timezone.now()
                    cs.save(update_fields=["last_attempt_at", "updated_at"])

        return Response({"status": "ok"})


@method_decorator(login_not_required, name="dispatch")
class ReconcileCommitView(APIView):
    """Agent confirms the module's post-flash version. Match -> ok; mismatch ->
    rolled_back (deterministic, 409), mirroring the deployment commit path."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ReconcileCommitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        convergence_id = serializer.validated_data["convergence_id"]
        version = serializer.validated_data["version"]

        with transaction.atomic():
            try:
                cs = (
                    ModuleFirmwareConvergenceState.objects.select_for_update()
                    .select_related("module", "target_release")
                    .get(pk=convergence_id)
                )
            except ModuleFirmwareConvergenceState.DoesNotExist:
                return Response(
                    {"detail": "Convergence not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            bound = cs.module.assignments.filter(station=station, to_ts__isnull=True).exists()
            if not bound:
                return Response(
                    {"detail": "Convergence not bound to this station."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            expected = cs.target_release.version
            if version != expected:
                # Bootloader rollback / version mismatch: treat as rolled_back,
                # deterministic, no retry (same policy as record_error).
                record_error(
                    cs,
                    ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK,
                    error_message=(f"Commit version {version!r} != target {expected!r}."),
                )
                _audit(
                    cs.module,
                    StationAuditLog.EventType.MODULE_FLASH_ROLLED_BACK,
                    f"Module {cs.module.uid} commit rejected: reports {version!r}, "
                    f"target {expected!r}.",
                    station=station,
                )
                return Response(
                    {"detail": "Version mismatch — recorded as rolled_back."},
                    status=status.HTTP_409_CONFLICT,
                )

            cs.state = ModuleFirmwareConvergenceState.State.OK
            cs.save(update_fields=["state", "updated_at"])
            module = cs.module
            if module.firmware_convergence != Module.Convergence.OK:
                module.firmware_convergence = Module.Convergence.OK
                module.save(update_fields=["firmware_convergence", "updated_at"])

        _audit(
            cs.module,
            StationAuditLog.EventType.MODULE_FLASH_SUCCESS,
            f"Module {cs.module.uid} committed version {version}.",
            station=station,
        )
        return Response({"status": "ok"})
