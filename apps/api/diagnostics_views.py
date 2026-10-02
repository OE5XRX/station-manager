"""REST endpoint for TX audio-path diagnostics.

POST /api/v1/stations/{pk}/audio-diagnostics/

Drives a headless diagnostic run via the channel-layer orchestrator and
returns the assembled run report.  Authentication mirrors the read API:
PersonalAccessToken or session, TopologyScopedPermission.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import PersonalAccessTokenAuthentication
from apps.api.permissions import TopologyScopedPermission
from apps.audio import orchestrator
from apps.stations.scoping import accessible_stations

_DEFAULT_SIGNAL = {
    "kind": "sine",
    "freq_hz": 1000,
    "level_dbfs": -20.0,
    "duration_ms": 500,
}

_VALID_ANCHORS = {"U", "C"}


class StationAudioDiagnosticView(APIView):
    """Run a TX audio-path diagnostic on a station agent."""

    authentication_classes = [PersonalAccessTokenAuthentication, SessionAuthentication]
    permission_classes = [TopologyScopedPermission]
    throttle_scope = "api-token"

    def post(self, request, pk):
        # Resolve station in the caller's scope (topology gate + 404 for out-of-scope).
        station = accessible_stations(request.user).filter(pk=pk).first()
        if station is None:
            raise NotFound()

        # --- Parse + validate request body -----------------------------------
        anchor = request.data.get("anchor", "U")
        if anchor not in _VALID_ANCHORS:
            raise ValidationError(
                {"anchor": f"Must be one of {sorted(_VALID_ANCHORS)}; got {anchor!r}."}
            )

        raw_slot = request.data.get("slot", 0)
        if not isinstance(raw_slot, int):
            raise ValidationError({"slot": "Must be an integer."})
        slot = raw_slot

        signal = request.data.get("signal") or _DEFAULT_SIGNAL

        # --- Run the orchestrator (sync wrapper around async) ----------------
        try:
            report = async_to_sync(orchestrator.run_headless_diagnostic)(
                station.pk, anchor, signal, slot=slot
            )
        except orchestrator.AgentNotConnected:
            return Response({"detail": "station agent not connected"}, status=503)
        except orchestrator.DiagnosticTimeout:
            return Response({"detail": "diagnostic timed out"}, status=504)

        return Response(report, status=200)
