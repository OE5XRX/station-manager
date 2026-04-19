import logging
import re

from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice
from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.serializers import (
    DeploymentCheckRequestSerializer,
    DeploymentCheckResponseSerializer,
    DeploymentCommitSerializer,
    DeploymentStatusUpdateSerializer,
)
from apps.images import storage as image_storage
from apps.stations.models import StationAuditLog

logger = logging.getLogger(__name__)


class DeploymentCheckView(APIView):
    """Station-agent polls to see if a deployment is pending for it."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        req = DeploymentCheckRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        logger.debug(
            "deployment check station=%s current_version=%r",
            station.pk,
            req.validated_data["current_version"],
        )

        result = (
            DeploymentResult.objects.filter(
                station=station,
                status=DeploymentResult.Status.PENDING,
                deployment__status=Deployment.Status.IN_PROGRESS,
            )
            .select_related("deployment__image_release")
            .order_by("deployment__created_at")
            .first()
        )

        if result is None:
            return Response(status=status.HTTP_204_NO_CONTENT)

        image = result.deployment.image_release
        data = DeploymentCheckResponseSerializer(
            {
                "deployment_result_id": result.pk,
                "deployment_id": result.deployment_id,
                "target_tag": image.tag,
                "checksum_sha256": image.sha256,
                "size_bytes": image.size_bytes,
                "download_url": f"/api/v1/deployments/{result.deployment_id}/download/",
            }
        ).data
        return Response(data)


class DeploymentStatusUpdateView(APIView):
    """Update the status of a deployment result (called by station agent)."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request, pk):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            result = DeploymentResult.objects.select_related("deployment__image_release").get(
                pk=pk, station=station
            )
        except DeploymentResult.DoesNotExist:
            return Response(
                {"detail": "Deployment result not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = DeploymentStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_status = serializer.validated_data["status"]
        error_message = serializer.validated_data.get("error_message", "")

        result.status = new_status
        update_fields = ["status"]

        if result.started_at is None and new_status != DeploymentResult.Status.PENDING:
            result.started_at = timezone.now()
            update_fields.append("started_at")

        if new_status in (DeploymentResult.Status.FAILED, DeploymentResult.Status.ROLLED_BACK):
            result.completed_at = timezone.now()
            result.error_message = error_message
            update_fields.extend(["completed_at", "error_message"])

        result.save(update_fields=update_fields)

        # Audit log
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
            message=f"Deployment #{result.deployment_id} status: {new_status}.",
        )

        # Check if deployment is complete after failure
        if new_status in (DeploymentResult.Status.FAILED, DeploymentResult.Status.ROLLED_BACK):
            _check_deployment_complete(result.deployment)

        # Broadcast update
        try:
            from apps.deployments.consumers import broadcast_deployment_status

            broadcast_deployment_status(result.deployment, result=result)
        except Exception:
            logger.exception("Failed to broadcast deployment status via WebSocket.")

        return Response({"status": "ok"})


class DeploymentCommitView(APIView):
    """Agent confirms boot committed after successful update."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = DeploymentCommitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        version = serializer.validated_data["version"]

        # Find the most recent in-progress result for this station
        result = (
            DeploymentResult.objects.filter(
                station=station,
                status__in=[
                    DeploymentResult.Status.REBOOTING,
                    DeploymentResult.Status.VERIFYING,
                    DeploymentResult.Status.INSTALLING,
                ],
            )
            .select_related("deployment")
            .order_by("-deployment__created_at")
            .first()
        )

        if result is None:
            return Response(
                {"detail": "No active deployment result found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        result.status = DeploymentResult.Status.SUCCESS
        result.completed_at = timezone.now()
        result.new_version = version
        result.save(update_fields=["status", "completed_at", "new_version"])

        # Audit log
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
            message=(
                f"Deployment #{result.deployment_id} committed. "
                f"Version: {result.previous_version} -> {version}."
            ),
        )

        # Check if the entire deployment is now complete
        _check_deployment_complete(result.deployment)

        # Broadcast update
        try:
            from apps.deployments.consumers import broadcast_deployment_status

            broadcast_deployment_status(result.deployment, result=result)
        except Exception:
            logger.exception("Failed to broadcast deployment status via WebSocket.")

        return Response({"status": "ok"})


class DeploymentDownloadView(APIView):
    """Stream the deployment's image from S3 to the requesting station.

    Authz: the station must have a non-terminal DeploymentResult for
    this deployment. Range requests are supported for resumable transfers.
    """

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    CHUNK = 1 << 20  # 1 MiB

    def get(self, request, pk):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        active_statuses = [
            DeploymentResult.Status.PENDING,
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
        ]
        result = (
            DeploymentResult.objects.select_related("deployment__image_release")
            .filter(deployment_id=pk, station=station, status__in=active_statuses)
            .first()
        )
        if result is None:
            return Response(
                {"detail": "No active deployment for this station on this deployment id."},
                status=status.HTTP_403_FORBIDDEN,
            )

        image = result.deployment.image_release
        stream = image_storage.open_stream(image.s3_key)
        total_size = image.size_bytes or 0

        # Optional Range support - translate HTTP Range into a seek on the stream.
        range_header = request.META.get("HTTP_RANGE", "")
        start = 0
        end = total_size - 1 if total_size else None
        http_status = 200
        length = total_size
        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end_g = m.group(2)
                if end_g:
                    end = int(end_g)
                try:
                    stream.seek(start)
                except Exception:
                    # Fallback: read-and-discard (default_storage backends
                    # don't all support seek; boto3 S3 does).
                    stream.close()
                    stream = image_storage.open_stream(image.s3_key)
                    discarded = 0
                    while discarded < start:
                        chunk = stream.read(min(self.CHUNK, start - discarded))
                        if not chunk:
                            break
                        discarded += len(chunk)
                http_status = 206
                length = (end - start + 1) if end is not None else None

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

        filename = f"oe5xrx-{image.machine}-{image.tag}.wic.bz2"
        safe_name = re.sub(r'["\r\n]', "_", filename) or "image.wic.bz2"
        response = StreamingHttpResponse(
            iterator(), status=http_status, content_type="application/x-bzip2"
        )
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        response["Accept-Ranges"] = "bytes"
        if length is not None:
            response["Content-Length"] = str(length)
        if http_status == 206 and end is not None:
            response["Content-Range"] = f"bytes {start}-{end}/{total_size or '*'}"
        return response


def _check_deployment_complete(deployment):
    """Check if all results are finished and update deployment status accordingly."""
    pending_or_active = deployment.results.filter(
        status__in=[
            DeploymentResult.Status.PENDING,
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
            DeploymentResult.Status.VERIFYING,
        ]
    ).exists()

    if not pending_or_active:
        has_failures = deployment.results.filter(
            status__in=[DeploymentResult.Status.FAILED, DeploymentResult.Status.ROLLED_BACK]
        ).exists()
        deployment.status = (
            Deployment.Status.FAILED if has_failures else Deployment.Status.COMPLETED
        )
        deployment.save(update_fields=["status", "updated_at"])
