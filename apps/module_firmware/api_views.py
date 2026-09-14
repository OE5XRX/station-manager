import io
import logging
import re

from django.contrib.auth.decorators import login_not_required
from django.http import StreamingHttpResponse
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice
from apps.module_firmware import storage
from apps.module_firmware.models import ModuleAssignmentHistory, ModuleFirmwareRelease

logger = logging.getLogger(__name__)


@method_decorator(login_not_required, name="dispatch")
class ModuleFirmwareDownloadView(APIView):
    """Stream a module firmware release to the requesting station.

    Authz: the station must have an open (to_ts__isnull=True)
    ModuleAssignmentHistory row whose module's module_type matches the
    requested release. All authz failures collapse to 403 so a station
    cannot probe release ids it does not own.

    Range requests are supported for resumable transfers (verbatim copy
    of the Range/streaming block from DeploymentDownloadView).
    """

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    CHUNK = 1 << 20  # 1 MiB

    def get(self, request, pk):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_403_FORBIDDEN,
            )

        release = ModuleFirmwareRelease.objects.filter(pk=pk).first()
        if release is None:
            return Response(
                {"detail": "Release not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        has_assignment = ModuleAssignmentHistory.objects.filter(
            station=station,
            to_ts__isnull=True,
            module__module_type_id=release.module_type_id,
        ).exists()
        if not has_assignment:
            return Response(
                {"detail": "No matching module assignment for this station."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            stream = storage.open_stream(release.storage_key)
        except Exception as exc:
            logger.error(
                "FW download: cannot open %s: %s",
                release.storage_key,
                exc,
            )
            return Response(
                {"detail": "Artifact unavailable from storage backend."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        total_size = release.size_bytes if release.size_bytes and release.size_bytes > 0 else None

        # Optional Range support - translate HTTP Range into a seek on the stream.
        range_header = request.META.get("HTTP_RANGE", "")
        start = 0
        end = (total_size - 1) if total_size is not None else None
        http_status = 200
        length = total_size
        if range_header:
            # Without a known total size we can't give a valid 206
            # (Content-Range requires complete-length) and can't
            # validate the client's bounds — refuse cleanly so the
            # agent falls back to a full GET.
            if total_size is None:
                if not getattr(stream, "closed", False):
                    stream.close()
                return Response(
                    {"detail": "Requested range not satisfiable."},
                    status=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                )
            unsatisfiable = False
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end_g = m.group(2)
                if end_g:
                    end = int(end_g)
                if start >= total_size:
                    unsatisfiable = True
                elif end is not None and end < start:
                    unsatisfiable = True
                else:
                    if end is not None:
                        end = min(end, total_size - 1)
                    try:
                        stream.seek(start)
                    except (AttributeError, io.UnsupportedOperation, NotImplementedError):
                        # Backend can't seek. A read-and-discard fallback
                        # turns every Range request into a full-object
                        # read on the wire, which is a bandwidth DoS
                        # vector from a compromised device. Refuse the
                        # range instead — the agent will restart from 0.
                        stream.close()
                        unsatisfiable = True
                    else:
                        http_status = 206
                        length = (end - start + 1) if end is not None else None

            if unsatisfiable:
                # Some storage backends return file-likes that only
                # implement close()/seekable() without a .closed
                # attribute — getattr keeps the 416 path from 500ing.
                if not getattr(stream, "closed", False):
                    stream.close()
                response = Response(
                    {"detail": "Requested range not satisfiable."},
                    status=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                )
                response["Content-Range"] = f"bytes */{total_size}"
                return response

        def iterator():
            remaining = length
            try:
                while True:
                    to_read = self.CHUNK if remaining is None else min(self.CHUNK, remaining)
                    if to_read <= 0:
                        break
                    chunk = stream.read(to_read)
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield chunk
            finally:
                stream.close()

        variant = release.variant or "x"
        filename = f"{release.module_type.key}-{variant}-{release.version}.signed.bin"
        safe_name = re.sub(r'["\r\n]', "_", filename) or "firmware.signed.bin"
        response = StreamingHttpResponse(
            iterator(), status=http_status, content_type="application/octet-stream"
        )
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        # Only advertise Range support if the underlying stream is
        # seekable — otherwise a client would reasonably try a Range
        # request and get a pointless 416 from the non-seekable path.
        if getattr(stream, "seekable", lambda: False)():
            response["Accept-Ranges"] = "bytes"
        else:
            response["Accept-Ranges"] = "none"
        if length is not None:
            response["Content-Length"] = str(length)
        if http_status == 206 and end is not None:
            response["Content-Range"] = f"bytes {start}-{end}/{total_size or '*'}"
        return response
