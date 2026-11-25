"""Agent-facing SSTV endpoints (DeviceKey-signed)."""

from django.contrib.auth.decorators import login_not_required
from django.http import FileResponse
from django.utils.decorators import method_decorator
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice

from .models import SstvTransmission


@method_decorator(login_not_required, name="dispatch")
class TxAudioView(APIView):
    """The audio of a transmission on the air, for the agent of its station only.

    403 unless the transmission belongs to the requesting device's station and is still
    ``sending``: an aborted/finished transmission can never be fetched (and keyed) again."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]
    permission_exempt = "device-key"

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    def get(self, request, tx_id):
        station = getattr(request.auth, "station", None)
        tx = SstvTransmission.objects.filter(pk=tx_id).first()
        if (
            station is None
            or tx is None
            or tx.station_id != station.pk
            or tx.state != SstvTransmission.State.SENDING
            or not tx.audio
        ):
            return Response({"detail": "Not available."}, status=403)
        response = FileResponse(tx.audio.open("rb"), content_type="application/octet-stream")
        response["Content-Length"] = str(tx.size)
        response["Cache-Control"] = "no-store"
        return response
