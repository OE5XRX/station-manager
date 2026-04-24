import io
import logging
import re

from django.db import transaction
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

        # Include the download/install-in-progress states so an agent that
        # crashed mid-flight can rediscover its own deployment on restart
        # and resume (download) or re-run (install). Terminal states stay
        # excluded — nothing to do from the agent's side.
        resumable_statuses = [
            DeploymentResult.Status.PENDING,
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
            DeploymentResult.Status.VERIFYING,
        ]
        # Newest-wins: if somehow two active results coexist (partial
        # supersession, race, data fix-up), the station should pick the
        # latest deployment to match what the admin intended. Matches
        # DeploymentCommitView's -deployment__created_at ordering.
        result = (
            DeploymentResult.objects.filter(
                station=station,
                status__in=resumable_statuses,
                deployment__status=Deployment.Status.IN_PROGRESS,
            )
            .select_related("deployment__image_release")
            .order_by("-deployment__created_at", "-pk")
            .first()
        )

        if result is None:
            return Response(status=status.HTTP_204_NO_CONTENT)

        image = result.deployment.image_release
        if not image.is_ota_ready:
            # Defense-in-depth: the creation-time guard in
            # UpgradeStationView/UpgradeGroupView should prevent
            # deployments from being created against non-OTA-ready
            # releases. If one still reached us (admin deleted the
            # field, data migration regression), refuse with 204
            # instead of 200 so the agent does not retry-loop on a
            # 409. The operator sees the Deployment row stuck in
            # PENDING and can investigate.
            logger.error(
                "DeploymentCheck: release %s is not OTA-ready; Deployment %d cannot proceed",
                image.tag,
                result.deployment_id,
            )
            return Response(status=status.HTTP_204_NO_CONTENT)

        data = DeploymentCheckResponseSerializer(
            {
                "deployment_result_id": result.pk,
                "deployment_id": result.deployment_id,
                "deployment_result_status": result.status,
                "target_tag": image.tag,
                "checksum_sha256": image.rootfs_sha256,
                "size_bytes": image.rootfs_size_bytes,
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

        # Audit log is best-effort — a transient DB hiccup here must
        # not 500 the endpoint after we've already persisted the status
        # update, or we'd skip _check_deployment_complete and the
        # WebSocket broadcast below and leave the dashboard stale.
        try:
            StationAuditLog.log(
                station=station,
                event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                message=f"Deployment #{result.deployment_id} status: {new_status}.",
            )
        except Exception:
            logger.warning(
                "Audit log write failed for deployment status update "
                "(station=%s, deployment=%s, status=%s)",
                station.pk,
                result.deployment_id,
                new_status,
                exc_info=True,
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
            .select_related("deployment__image_release")
            .order_by("-deployment__created_at")
            .first()
        )

        if result is None:
            return Response(
                {"detail": "No active deployment result found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        expected_tag = result.deployment.image_release.tag
        if version != expected_tag:
            # Agent is running a different image than the one this
            # deployment targets — most likely a bootloader rollback
            # after a failed trial boot. Don't mark SUCCESS, don't move
            # the station's current_image_release pointer; record the
            # mismatch as rolled_back so the dashboard shows reality.
            result.status = DeploymentResult.Status.ROLLED_BACK
            result.completed_at = timezone.now()
            result.new_version = version
            result.error_message = (
                f"Commit version {version!r} does not match deployment "
                f"target {expected_tag!r}; treating as bootloader rollback."
            )
            result.save(update_fields=["status", "completed_at", "new_version", "error_message"])
            # Audit log is best-effort — a transient DB hiccup here must
            # not turn the deterministic 409 response into a 500 after
            # we've already mutated the DeploymentResult row.
            try:
                StationAuditLog.log(
                    station=station,
                    event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                    message=(
                        f"Deployment #{result.deployment_id} commit rejected: "
                        f"station reports {version!r}, target was {expected_tag!r}."
                    ),
                )
            except Exception:
                logger.warning(
                    "Audit log write failed for commit mismatch "
                    "(station=%s, deployment=%s, reported=%r, expected=%r)",
                    station.pk,
                    result.deployment_id,
                    version,
                    expected_tag,
                    exc_info=True,
                )
            _check_deployment_complete(result.deployment)
            # Broadcast the rolled_back transition so the upgrade
            # dashboard and deployment-detail page reflect the
            # terminal state immediately — otherwise the UI would sit
            # on "installing" until the next unrelated event.
            try:
                from apps.deployments.consumers import broadcast_deployment_status

                broadcast_deployment_status(result.deployment, result=result)
            except Exception:
                logger.exception("Failed to broadcast rolled_back via WebSocket.")
            return Response(
                {"detail": "Version mismatch — recorded as rolled_back."},
                status=status.HTTP_409_CONFLICT,
            )

        # Persist the SUCCESS transition and the Station pointer move
        # in a single atomic block. Without it, a DB error between the
        # two saves leaves the result marked SUCCESS but the station
        # still pointing at the old image — and because the agent-retry
        # lookup only matches REBOOTING/VERIFYING/INSTALLING, a retry
        # from the station would 404 and never re-sync the pointer.
        with transaction.atomic():
            result.status = DeploymentResult.Status.SUCCESS
            result.completed_at = timezone.now()
            result.new_version = version
            result.save(update_fields=["status", "completed_at", "new_version"])

            # Update the station's "provisioned with" pointer so the UI reflects
            # what's running on disk right now.
            station.current_image_release = result.deployment.image_release
            station.updated_at = timezone.now()
            station.save(update_fields=["current_image_release", "updated_at"])

        # Audit log is best-effort — a transient DB hiccup on the audit
        # table must not 500 after we've already committed the success.
        try:
            StationAuditLog.log(
                station=station,
                event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                message=(
                    f"Deployment #{result.deployment_id} committed. "
                    f"Version: {result.previous_version} -> {version}."
                ),
            )
        except Exception:
            logger.warning(
                "Audit log write failed for deployment commit "
                "(station=%s, deployment=%s, version=%r)",
                station.pk,
                result.deployment_id,
                version,
                exc_info=True,
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
    this deployment. All authz failures (missing deployment, wrong
    station, terminal status) collapse to 403 so a station cannot
    probe deployment ids it does not own.

    Range requests are supported for resumable transfers.
    """

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    CHUNK = 1 << 20  # 1 MiB

    def get(self, request, pk):
        station = getattr(request.auth, "station", None)
        if station is None:
            # Docstring promises authz failures uniformly collapse to 403
            # so a station can't distinguish "my key has no station" from
            # "that deployment isn't mine". Honour that here too.
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Mirror the check view's resumable set. A station that crashed
        # in VERIFYING and restarted will re-enter the flow and hit the
        # download endpoint again; the idempotent re-download is a
        # cheap way to unblock recovery compared with stuck deployments
        # that need admin intervention.
        active_statuses = [
            DeploymentResult.Status.PENDING,
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
            DeploymentResult.Status.VERIFYING,
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
        if not image.is_ota_ready:
            # Defense-in-depth — the creation-time guard should keep
            # us out of this branch, but we refuse rather than stream
            # the full wic (which is 4× the target slot size) if
            # something regressed.
            logger.error(
                "DeploymentDownload: release %s is not OTA-ready; deployment %d cannot be served",
                image.tag,
                result.deployment_id,
            )
            return Response(
                {"detail": "Release not prepared for OTA."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Fail loud but controlled on storage issues — a 500 without a
        # response body makes agents retry blindly and leaves operators
        # poking stack traces in sentry. Map missing-key / permission
        # errors to 502 Bad Gateway (server knows about the deployment,
        # the upstream object store doesn't have what we need).
        try:
            stream = image_storage.open_stream(image.rootfs_s3_key)
        except Exception as exc:
            logger.error(
                "Failed to open rootfs %s for deployment %s: %s",
                image.rootfs_s3_key,
                result.deployment_id,
                exc,
            )
            return Response(
                {"detail": "Image artifact unavailable from storage backend."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        # Treat missing / non-positive rootfs_size_bytes as unknown. If we
        # coerced it to 0, every response would advertise
        # Content-Length: 0 and the Range math would reject every
        # request as start >= 0, even though the stream actually has
        # bytes to serve.
        total_size = (
            image.rootfs_size_bytes
            if image.rootfs_size_bytes and image.rootfs_size_bytes > 0
            else None
        )

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

        filename = f"oe5xrx-{image.machine}-{image.tag}.rootfs.bz2"
        safe_name = re.sub(r'["\r\n]', "_", filename) or "image.rootfs.bz2"
        response = StreamingHttpResponse(
            iterator(), status=http_status, content_type="application/x-bzip2"
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
