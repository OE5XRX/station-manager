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
from apps.control.models import ControlLock
from apps.stations.models import StationAuditLog

from .drain import ARTIFACT_SIM, DFU_STUCK_PREFIX, executable_artifact
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

    Idle gate: while any ControlLock of the station is held, nothing is handed
    out (204) — a flash never interrupts a running operator session.

    Artifact selection: synthetic (sim) modules get the native_sim artifact,
    real hardware the signed DFU image. A module is skipped when the agent does
    not report the matching ``module_ota`` backend or the target release lacks
    that artifact.
    """

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]
    permission_exempt = "device-key"

    def post(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        req = ReconcileCheckRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)

        # Idle gate — evaluated BEFORE reconciling (any scope counts).
        if ControlLock.objects.filter(station=station, holder__isnull=False).exists():
            return Response(status=status.HTTP_204_NO_CONTENT)

        backends = station.module_ota_backends()

        assignments = (
            ModuleAssignmentHistory.objects.filter(station=station, to_ts__isnull=True)
            .select_related("module", "module__module_type")
            .order_by("slot")
        )

        resume = None  # a mid-flight updating instruction
        first_drift = None  # first fresh-drift instruction

        for a in assignments:
            module = a.module
            cs = reconcile_module(module)
            if cs is None or cs.state != ModuleFirmwareConvergenceState.State.UPDATING:
                continue
            artifact = executable_artifact(module, cs.target_release, backends)
            if artifact is None:
                continue
            instruction = self._instruction(module, a.slot, cs, artifact)
            # A row is a genuine "resume" only once the agent has actually
            # started flashing it — the status endpoint stamps last_attempt_at on
            # the first progress POST. Heartbeat ingestion creates an updating row
            # for EVERY drifted module, so an existence check would mark all of
            # them "pre-existing" and degenerate the preference to slot order.
            if cs.last_attempt_at is not None:
                resume = resume or instruction
            else:
                first_drift = first_drift or instruction

        chosen = resume or first_drift
        if chosen is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(ReconcileCheckResponseSerializer(chosen).data)

    def _instruction(self, module, slot, cs, artifact):
        release = cs.target_release
        download_url = reverse("module_firmware_api:download", args=[release.pk])
        if artifact == ARTIFACT_SIM:
            download_url += "?artifact=sim"
            checksum, size = release.sim_sha256, release.sim_size_bytes
        else:
            checksum, size = release.sha256, release.size_bytes
        return {
            "convergence_id": cs.pk,
            "module_uid": module.uid,
            "slot": slot,
            "module_type": module.module_type.key,
            "variant": module.variant,
            "target_version": release.version,
            "artifact": artifact,
            "download_url": download_url,
            "checksum_sha256": checksum,
            "size_bytes": size,
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
    permission_exempt = "device-key"

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
            # Global lock order module → assignment → convergence (see R2-1). Fetch
            # the convergence row UNLOCKED first, only to learn its module_id, then
            # acquire the row locks in the global order.
            cs0 = (
                ModuleFirmwareConvergenceState.objects.select_related("module")
                .filter(pk=convergence_id)
                .first()
            )
            if cs0 is None:
                return Response(
                    {"detail": "Convergence not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # 1) Lock the module row FIRST.
            Module.objects.select_for_update().filter(pk=cs0.module_id).first()

            # 2) Authz: the module must be currently assigned to THIS station. Lock
            # + re-read the open assignment (after the module lock, per the global
            # order) so a concurrent reassignment can't close it between check and
            # mutation.
            open_assignment = (
                ModuleAssignmentHistory.objects.select_for_update()
                .filter(module_id=cs0.module_id, station=station, to_ts__isnull=True)
                .first()
            )
            if open_assignment is None:
                return Response(
                    {"detail": "Convergence not bound to this station."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # 3) Lock + re-read the convergence row LAST.
            cs = (
                ModuleFirmwareConvergenceState.objects.select_for_update()
                .select_related("module")
                .get(pk=convergence_id)
            )

            # Terminal rows accept no further callbacks: a quarantined row must
            # never be reactivated, and after a successful commit sets OK a
            # delayed rejected/rolled_back/progress callback must not alter it.
            if cs.state in (
                ModuleFirmwareConvergenceState.State.QUARANTINED,
                ModuleFirmwareConvergenceState.State.OK,
            ):
                return Response(
                    {"detail": "Convergence is terminal; no updates accepted."},
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
                # progress (downloading/flashing/verifying). ``verifying`` means the
                # image was written: real progress, so the drain's transient cap
                # restarts (downloading/flashing are not proof the next step works).
                if new_status == "verifying" and cs.transient_failures:
                    cs.transient_failures = 0
                    cs.save(update_fields=["transient_failures", "updated_at"])
                # Any progress status means the module came back out of DFU (the
                # cycle restarted on CDC): the DFU-stuck notice is stale.
                if cs.last_error_message.startswith(DFU_STUCK_PREFIX):
                    cs.last_error_message = ""
                    cs.save(update_fields=["last_error_message", "updated_at"])
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
    permission_exempt = "device-key"

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
            # Global lock order module → assignment → convergence (see R2-1). Fetch
            # the convergence row UNLOCKED first, only to learn its module_id, then
            # acquire the row locks in the global order.
            cs0 = (
                ModuleFirmwareConvergenceState.objects.select_related("module")
                .filter(pk=convergence_id)
                .first()
            )
            if cs0 is None:
                return Response(
                    {"detail": "Convergence not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # 1) Lock the module row FIRST.
            Module.objects.select_for_update().filter(pk=cs0.module_id).first()

            # 2) Lock + re-read the open assignment (after the module lock, per the
            # global order) so a concurrent reassignment can't close it between
            # check and mutation.
            open_assignment = (
                ModuleAssignmentHistory.objects.select_for_update()
                .filter(module_id=cs0.module_id, station=station, to_ts__isnull=True)
                .first()
            )
            if open_assignment is None:
                return Response(
                    {"detail": "Convergence not bound to this station."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # 3) Lock + re-read the convergence row LAST.
            cs = (
                ModuleFirmwareConvergenceState.objects.select_for_update()
                .select_related("module", "target_release")
                .get(pk=convergence_id)
            )

            # A QUARANTINED row has been GIVEN UP — a delayed commit against it must
            # not be reported as success (the real bug: the API would otherwise emit
            # MODULE_FLASH_SUCCESS for a rejected instruction). Only quarantine is
            # guarded here. Unlike the STATUS endpoint (which also rejects OK, since
            # a late rejected/rolled_back callback must not mutate a converged row),
            # an OK row at commit time means the module already converged (e.g. a
            # heartbeat reported the target version before the agent's commit
            # landed). The commit is then a legitimate idempotent confirmation and
            # must proceed to 200 — conflating "already succeeded" with "given up"
            # would make a successful flash look rejected to the agent / Teilbereich D.
            if cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
                return Response(
                    {"detail": "Convergence is quarantined; no updates accepted."},
                    status=status.HTTP_409_CONFLICT,
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

            # Payload matches the target — but never trust the body for the final
            # state. Audit the accepted commit, then DERIVE convergence from the
            # module's real last_reported_version (crash-safe principle). If the
            # heartbeat has caught up, reconcile_module lands OK; otherwise it
            # stays updating and the next check re-offers it. reconcile_module
            # maintains both the row state and the Module rollup itself.
            _audit(
                cs.module,
                StationAuditLog.EventType.MODULE_FLASH_SUCCESS,
                f"Module {cs.module.uid} committed version {version}.",
                station=station,
            )
            if cs.transient_failures:
                cs.transient_failures = 0
                cs.save(update_fields=["transient_failures", "updated_at"])
            reconcile_module(cs.module)

        return Response({"status": "ok"})
