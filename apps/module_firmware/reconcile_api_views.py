import logging

from django.contrib.auth.decorators import login_not_required
from django.urls import reverse
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice

from .models import ModuleAssignmentHistory, ModuleFirmwareConvergenceState
from .reconciler import reconcile_module
from .reconcile_serializers import (
    ReconcileCheckRequestSerializer,
    ReconcileCheckResponseSerializer,
)

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
