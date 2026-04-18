import logging
import re

from django.contrib import messages
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.images import storage as image_storage
from apps.stations.models import Station, StationAuditLog

from .forms import ProvisioningForm
from .models import ProvisioningJob

logger = logging.getLogger(__name__)

ACTIVE_PROVISIONING_STATUSES = [
    ProvisioningJob.Status.PENDING,
    ProvisioningJob.Status.RUNNING,
    ProvisioningJob.Status.READY,
]


class CreateProvisioningJobView(AdminRequiredMixin, View):
    """Admin-only endpoint that creates a new ProvisioningJob for a station."""

    def post(self, request, station_pk):
        station = get_object_or_404(Station, pk=station_pk)
        form = ProvisioningForm(request.POST)
        if not form.is_valid():
            return HttpResponse(_("invalid form"), status=400)
        image_release = form.cleaned_data["image_release"]
        # The template posts `machine` alongside `image_release` for UI
        # grouping/filtering. If present, it must match the image's machine;
        # a mismatch means the UI state and the posted image disagree
        # (broken JS, autofill, or a hand-crafted POST) and we should reject
        # rather than silently provisioning something the operator didn't
        # expect. If absent (JS disabled, no dropdown rendered), accept —
        # the image_release is still the source of truth.
        posted_machine = request.POST.get("machine", "")
        if posted_machine and posted_machine != image_release.machine:
            messages.error(
                request,
                _(
                    "Selected machine (%(machine)s) does not match the image's "
                    "machine (%(image_machine)s)."
                )
                % {
                    "machine": posted_machine,
                    "image_machine": image_release.machine,
                },
            )
            return redirect("stations:station_detail", pk=station.pk)
        if ProvisioningJob.objects.filter(
            station=station, status__in=ACTIVE_PROVISIONING_STATUSES
        ).exists():
            messages.error(request, _("This station already has an active provisioning job."))
            return redirect("stations:station_detail", pk=station.pk)
        ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=request.user,
        )
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.PROVISIONING_REQUESTED,
            message=(
                f"Provisioning bundle requested for {image_release.get_machine_display()} "
                f"{image_release.tag} by {request.user.username}"
            ),
            user=request.user,
        )
        return redirect("stations:station_detail", pk=station.pk)


class ProvisioningJobStatusView(AdminRequiredMixin, View):
    """HTMX-polled partial showing the job's current state."""

    def get(self, request, pk):
        job = get_object_or_404(ProvisioningJob, pk=pk)
        return render(request, "provisioning/_job_status.html", {"job": job})


class ProvisioningJobDownloadView(AdminRequiredMixin, View):
    """Stream the provisioned image from S3 and mark job as downloaded.

    Returns 409 if the job is not yet READY and 410 if its presigned-like
    expiry window has lapsed. The DOWNLOADED status transition only occurs
    after the response generator has streamed the full object, and uses a
    conditional UPDATE so concurrent downloads can't clobber a FAILED /
    EXPIRED state written by the worker between status check and iteration.
    """

    CHUNK = 1 << 20  # 1 MiB

    def get(self, request, pk):
        job = get_object_or_404(ProvisioningJob, pk=pk)
        if job.status != ProvisioningJob.Status.READY:
            return HttpResponse(_("not ready"), status=409)
        if job.expires_at and job.expires_at < timezone.now():
            return HttpResponse(_("expired"), status=410)

        stream = image_storage.open_stream(job.output_s3_key)
        chunk_size = self.CHUNK
        job_pk = job.pk
        station_pk = job.station_id
        image_release_pk = job.image_release_id
        image_release_tag = job.image_release.tag
        user = request.user

        def iterator():
            try:
                try:
                    while chunk := stream.read(chunk_size):
                        yield chunk
                except GeneratorExit:
                    # Client closed the connection mid-stream; do NOT mark
                    # DOWNLOADED — the bundle may not have been received.
                    raise
                else:
                    # Full stream consumed — only now count it as a
                    # successful download. Conditional UPDATE preserves
                    # any FAILED / EXPIRED transition written concurrently.
                    updated = ProvisioningJob.objects.filter(
                        pk=job_pk,
                        status=ProvisioningJob.Status.READY,
                    ).update(
                        status=ProvisioningJob.Status.DOWNLOADED,
                        downloaded_at=timezone.now(),
                    )
                    if updated:
                        # Record which image release is now on the station,
                        # and log the download. Use .filter().update() for
                        # the station write to stay consistent with the
                        # generator-safe DB access pattern used above.
                        # QuerySet.update() bypasses auto_now, so bump
                        # updated_at explicitly — the station-list UI uses
                        # it to show "last changed" timestamps.
                        Station.objects.filter(pk=station_pk).update(
                            current_image_release_id=image_release_pk,
                            updated_at=timezone.now(),
                        )
                        station = Station.objects.filter(pk=station_pk).first()
                        if station is not None:
                            # Audit logging is ancillary observability — a
                            # transient DB failure here must not turn a
                            # fully-streamed download into a 500. The status
                            # transition above is the authoritative record.
                            try:
                                StationAuditLog.log(
                                    station=station,
                                    event_type=(StationAuditLog.EventType.PROVISIONING_DOWNLOADED),
                                    message=(
                                        f"Provisioning bundle downloaded "
                                        f"({image_release_tag}) by {user.username}"
                                    ),
                                    user=user,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Audit log write failed for station %s "
                                    "(provisioning_downloaded): %s",
                                    station_pk,
                                    exc,
                                )
            finally:
                stream.close()

        # Sanitize filename for Content-Disposition header — the s3 key embeds
        # the user-supplied image tag, which could contain quotes or CRLF that
        # would break the header or allow response-splitting. Matches the
        # pattern used in apps/firmware/views.py FirmwareDownloadView.
        raw_name = job.output_s3_key.split("/")[-1]
        filename = re.sub(r'["\r\n]', "_", raw_name) or "download.wic.bz2"
        response = StreamingHttpResponse(iterator(), content_type="application/x-bzip2")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        if job.output_size_bytes:
            response["Content-Length"] = str(job.output_size_bytes)
        return response
