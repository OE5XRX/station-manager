import logging

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
    """Interim stub. Returns 501; S3-backed download lands in Task 8."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def get(self, request, pk):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not DeploymentResult.objects.filter(pk=pk, station=station).exists():
            return Response(
                {"detail": "Deployment result not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {"detail": "Image download via S3 is not yet implemented."},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


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
