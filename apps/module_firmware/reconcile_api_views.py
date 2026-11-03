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
from .mcuboot import KEY_UNKNOWN_HINT, key_hint_applies
from .models import Module, ModuleAssignmentHistory, ModuleFirmwareConvergenceState
from .reconcile_serializers import (
    ReconcileCheckRequestSerializer,
    ReconcileCheckResponseSerializer,
    ReconcileCommitSerializer,
    ReconcileStatusSerializer,
)
from .reconciler import _audit, reconcile_module, record_error

logger = logging.getLogger(__name__)

State = ModuleFirmwareConvergenceState.State

# Rows the reconciler has given up on: no callback may mutate them (a late
# rolled_back/rejected would otherwise re-run record_error and turn a FAILED /
# NOT_UPDATABLE row into QUARANTINED). Each rejected call is audited as CONFLICT.
_GIVEN_UP = (State.QUARANTINED, State.FAILED, State.NOT_UPDATABLE)


def _conflict(cs, what, station):
    _audit(
        cs.module,
        StationAuditLog.EventType.MODULE_FLASH_CONFLICT,
        f"Modul {cs.module.uid}: {what} abgelehnt – Zustand {cs.state}.",
        station=station,
    )
    return Response(
        {"detail": f"Convergence is {cs.state}; no updates accepted."},
        status=status.HTTP_409_CONFLICT,
    )


_E = StationAuditLog.EventType
# Flash-lifecycle events that bound an attempt for the commit dedupe below.
_FLASH_LIFECYCLE = (
    _E.MODULE_FLASH_INSTRUCTED,
    _E.MODULE_FLASH_STARTED,
    _E.MODULE_FLASH_PHASE,
    _E.MODULE_FLASH_SUCCESS,
    _E.MODULE_FLASH_FAILED,
    _E.MODULE_FLASH_ROLLED_BACK,
    _E.MODULE_FLASH_REJECTED,
    _E.MODULE_FLASH_GAVE_UP,
    _E.MODULE_FLASH_RETRY,
    _E.MODULE_FLASH_CONFLICT,
)


def _is_repeated_commit(module, success_message):
    """True if the module's newest flash-lifecycle audit entry is already the
    SUCCESS for this very version (spec §11.2: a re-sent identical commit, e.g.
    from the agent's marker recovery, logs no second SUCCESS). Row state cannot
    tell first from repeated commit — a heartbeat may have reconciled the row to
    OK before the first commit lands — so the audit trail is the attempt
    boundary: any new INSTRUCTED/STARTED/... entry in between means a new
    attempt, whose commit is logged again. One query (module FK index)."""
    newest = (
        StationAuditLog.objects.filter(module=module, event_type__in=_FLASH_LIFECYCLE)
        .order_by("-created_at", "-pk")
        .values_list("event_type", "message")
        .first()
    )
    return newest == (_E.MODULE_FLASH_SUCCESS, success_message)


def _with_key_hint(message, cs, error_mode):
    if key_hint_applies(cs.module, cs.target_release, error_mode):
        return f"{message} {KEY_UNKNOWN_HINT}"
    return message


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
                resume = resume or (instruction, cs)
            else:
                first_drift = first_drift or (instruction, cs)

        chosen = resume or first_drift
        if chosen is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        instruction, cs = chosen
        self._mark_instructed(cs, instruction, station)
        return Response(ReconcileCheckResponseSerializer(instruction).data)

    @staticmethod
    def _mark_instructed(cs, instruction, station):
        """Audit INSTRUCTED once per open instruction. ``instructed_at`` is the
        dedup marker: only the poll that flips it from NULL logs (a conditional
        UPDATE, so concurrent polls cannot both win). A terminal status / commit
        clears it, so the next attempt's instruction logs again. A fresh
        instruction also clears a stale phase left by a lost attempt."""
        rows = ModuleFirmwareConvergenceState.objects.filter(
            pk=cs.pk, instructed_at__isnull=True
        ).update(instructed_at=timezone.now(), phase="", phase_at=None)
        if rows == 1:
            _audit(
                cs.module,
                StationAuditLog.EventType.MODULE_FLASH_INSTRUCTED,
                f"Modul {instruction['module_uid']}: Update-Anweisung auf "
                f"{instruction['target_version']} ausgegeben ({instruction['artifact']}).",
                station=station,
            )

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

            # Terminal rows accept no further callbacks: a given-up row
            # (quarantined / failed / not_updatable) must never be reactivated or
            # re-judged (audited as CONFLICT), and after a successful commit sets
            # OK a delayed rejected/rolled_back/progress callback must not alter
            # it (a benign late callback, not audited).
            if cs.state in _GIVEN_UP:
                return _conflict(cs, f"Status '{new_status}'", station)
            if cs.state == State.OK:
                return Response(
                    {"detail": "Convergence is terminal; no updates accepted."},
                    status=status.HTTP_409_CONFLICT,
                )

            error_mode = _STATUS_TO_ERROR.get(new_status)
            if error_mode is not None:
                # record_error also ends the attempt (clears phase, instructed_at).
                record_error(cs, error_mode, error_message=error_message, station=station)
                _audit(
                    cs.module,
                    _STATUS_TO_AUDIT[new_status],
                    _with_key_hint(
                        f"Module {cs.module.uid} flash {new_status} "
                        f"(target {cs.target_release.version}).",
                        cs,
                        error_mode,
                    ),
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
                # Phase tracking: audit per phase CHANGE only. The first phase of
                # an attempt (phase still "") is the attempt start (STARTED); later
                # changes are PHASE. A repeated phase writes and logs nothing.
                old_phase = cs.phase
                if new_status != old_phase:
                    now = timezone.now()
                    cs.phase = new_status
                    cs.phase_at = now
                    fields = ["phase", "phase_at", "updated_at"]
                    if old_phase == "":
                        _audit(
                            cs.module,
                            StationAuditLog.EventType.MODULE_FLASH_STARTED,
                            f"Module {cs.module.uid} flash started "
                            f"(target {cs.target_release.version}).",
                            station=station,
                        )
                        # last_attempt_at marks the row as "resume" for check.
                        if cs.last_attempt_at is None:
                            cs.last_attempt_at = now
                            fields.append("last_attempt_at")
                    else:
                        _audit(
                            cs.module,
                            StationAuditLog.EventType.MODULE_FLASH_PHASE,
                            f"Modul {cs.module.uid}: Phase {old_phase} → {new_status}.",
                            station=station,
                        )
                    cs.save(update_fields=fields)

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

            # A given-up row (quarantined / failed / not_updatable) — a delayed
            # commit against it must not be reported as success (the real bug: the
            # API would otherwise emit MODULE_FLASH_SUCCESS for a rejected
            # instruction) nor re-judged as rolled_back; each rejected call is
            # audited as CONFLICT. Only given-up states are guarded here. Unlike
            # the STATUS endpoint (which also rejects OK, since
            # a late rejected/rolled_back callback must not mutate a converged row),
            # an OK row at commit time means the module already converged (e.g. a
            # heartbeat reported the target version before the agent's commit
            # landed). The commit is then a legitimate idempotent confirmation and
            # must proceed to 200 — conflating "already succeeded" with "given up"
            # would make a successful flash look rejected to the agent / Teilbereich D.
            if cs.state in _GIVEN_UP:
                return _conflict(cs, f"Commit '{version}'", station)

            expected = cs.target_release.version
            if version != expected:
                # Bootloader rollback / version mismatch: treat as rolled_back,
                # deterministic, no retry (same policy as record_error).
                record_error(
                    cs,
                    ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK,
                    error_message=(f"Commit version {version!r} != target {expected!r}."),
                    station=station,
                )
                _audit(
                    cs.module,
                    StationAuditLog.EventType.MODULE_FLASH_ROLLED_BACK,
                    _with_key_hint(
                        f"Module {cs.module.uid} commit rejected: reports {version!r}, "
                        f"target {expected!r}.",
                        cs,
                        ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK,
                    ),
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
            success_message = f"Module {cs.module.uid} committed version {version}."
            if not _is_repeated_commit(cs.module, success_message):
                _audit(
                    cs.module,
                    StationAuditLog.EventType.MODULE_FLASH_SUCCESS,
                    success_message,
                    station=station,
                )
            # The attempt is over: reset the drain cap and the in-flight
            # bookkeeping (phase / instruction marker) before deriving the state.
            cs.transient_failures = 0
            cs.phase = ""
            cs.phase_at = None
            cs.instructed_at = None
            cs.save(
                update_fields=[
                    "transient_failures",
                    "phase",
                    "phase_at",
                    "instructed_at",
                    "updated_at",
                ]
            )
            reconcile_module(cs.module)

        return Response({"status": "ok"})
