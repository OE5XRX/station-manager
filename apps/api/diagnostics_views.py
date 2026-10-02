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

_VALID_ANCHORS = {"C"}

_SIGNAL_DURATION_MAX_MS: int = 5000


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
        anchor = request.data.get("anchor", "C")
        if anchor == "U":
            raise ValidationError(
                {
                    "anchor": (
                        "anchor 'U' (server-originated headless reference) is experimental"
                        " and validated on-station only; use anchor 'C'"
                    )
                }
            )
        if anchor not in _VALID_ANCHORS:
            raise ValidationError(
                {"anchor": f"Must be one of {sorted(_VALID_ANCHORS)}; got {anchor!r}."}
            )

        raw_slot = request.data.get("slot", 0)
        if not isinstance(raw_slot, int) or isinstance(raw_slot, bool):
            raise ValidationError({"slot": "Must be an integer."})
        slot = raw_slot

        raw_signal = request.data.get("signal")
        if raw_signal is None:
            signal = _DEFAULT_SIGNAL
        else:
            if not isinstance(raw_signal, dict):
                raise ValidationError({"signal": "Must be a JSON object (dict)."})
            kind = raw_signal.get("kind", "sine")
            if kind != "sine":
                raise ValidationError(
                    {
                        "signal": {
                            "kind": f"Unsupported signal kind {kind!r}; only 'sine' is supported."
                        }
                    }
                )
            if "freq_hz" in raw_signal:
                freq = raw_signal["freq_hz"]
                if not isinstance(freq, int) or isinstance(freq, bool) or freq <= 0:
                    raise ValidationError({"signal": {"freq_hz": "Must be a positive integer."}})
            if "level_dbfs" in raw_signal:
                level = raw_signal["level_dbfs"]
                if not isinstance(level, (int, float)) or isinstance(level, bool) or level > 0:
                    raise ValidationError({"signal": {"level_dbfs": "Must be a number ≤ 0."}})
            if "duration_ms" in raw_signal:
                dur = raw_signal["duration_ms"]
                if not isinstance(dur, int) or isinstance(dur, bool) or dur <= 0:
                    raise ValidationError(
                        {"signal": {"duration_ms": "Must be a positive integer."}}
                    )
                if dur > _SIGNAL_DURATION_MAX_MS:
                    raise ValidationError(
                        {
                            "signal": {
                                "duration_ms": (f"Must not exceed {_SIGNAL_DURATION_MAX_MS} ms.")
                            }
                        }
                    )
            signal = {**_DEFAULT_SIGNAL, **raw_signal}

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
