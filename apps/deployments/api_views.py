import logging
import re

from django.http import FileResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice
from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.serializers import (
    DeploymentCheckResponseSerializer,
    DeploymentCommitSerializer,
    DeploymentStatusUpdateSerializer,
)
from apps.firmware.models import FirmwareDelta
from apps.stations.models import StationAuditLog

logger = logging.getLogger(__name__)


class DeploymentCheckView(APIView):
    """Check if there is a pending deployment for the authenticated station."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def get(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        result = (
            DeploymentResult.objects.filter(
                station=station,
                status=DeploymentResult.Status.PENDING,
                deployment__status=Deployment.Status.IN_PROGRESS,
            )
            .select_related("deployment__firmware_artifact")
            .order_by("deployment__created_at")
            .first()
        )

        if result is None:
            return Response(status=status.HTTP_204_NO_CONTENT)

        artifact = result.deployment.firmware_artifact

        # Check if a delta is available for this station's current version
        delta_data = {
            "is_delta": False,
            "delta_checksum_sha256": "",
            "delta_file_size": 0,
        }
        current_version = getattr(station, "current_os_version", "") or ""
        if current_version:
            delta = FirmwareDelta.objects.filter(
                source_artifact__version=current_version,
                source_artifact__name=artifact.name,
                target_artifact=artifact,
            ).first()
            if delta:
                delta_data = {
                    "is_delta": True,
                    "delta_checksum_sha256": delta.checksum_sha256,
                    "delta_file_size": delta.delta_size,
                }

        download_url = f"/api/v1/deployments/{result.pk}/download/"
        if delta_data["is_delta"]:
            download_url += "?delta=true"

        data = DeploymentCheckResponseSerializer(
            {
                "result_id": result.pk,
                "deployment_id": result.deployment_id,
                "firmware_name": artifact.name,
                "firmware_version": artifact.version,
                "download_url": download_url,
                "checksum_sha256": artifact.checksum_sha256,
                "file_size": artifact.file_size,
                **delta_data,
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
            result = DeploymentResult.objects.select_related("deployment__firmware_artifact").get(
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
    """Serve the firmware file for a deployment result."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def get(self, request, pk):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            result = DeploymentResult.objects.select_related("deployment__firmware_artifact").get(
                pk=pk, station=station
            )
        except DeploymentResult.DoesNotExist:
            return Response(
                {"detail": "Deployment result not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        artifact = result.deployment.firmware_artifact
        serve_delta = request.query_params.get("delta", "").lower() == "true"

        if serve_delta:
            current_version = getattr(result.station, "current_os_version", "") or ""
            delta = None
            if current_version:
                delta = FirmwareDelta.objects.filter(
                    source_artifact__version=current_version,
                    source_artifact__name=artifact.name,
                    target_artifact=artifact,
                ).first()

            if delta:
                response = FileResponse(
                    delta.delta_file.open("rb"),
                    content_type="application/octet-stream",
                )
                safe_name = re.sub(
                    r'["\r\n]',
                    "_",
                    f"{artifact.name}-{current_version}_to_{artifact.version}.xdelta3",
                )
                response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
                return response

            # Fall through to full image if delta not found
            logger.warning(
                "Delta requested but not found for station %s (version %s -> %s). "
                "Serving full image.",
                result.station,
                current_version,
                artifact.version,
            )

        response = FileResponse(
            artifact.file.open("rb"),
            content_type="application/octet-stream",
        )
        safe_name = re.sub(
            r'["\r\n]',
            "_",
            f"{artifact.name}-v{artifact.version}",
        )
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
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
