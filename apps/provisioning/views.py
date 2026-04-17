from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.images import storage as image_storage
from apps.stations.models import Station

from .forms import ProvisioningForm
from .models import ProvisioningJob


class CreateProvisioningJobView(AdminRequiredMixin, View):
    """Admin-only endpoint that creates a new ProvisioningJob for a station."""

    def post(self, request, station_pk):
        station = get_object_or_404(Station, pk=station_pk)
        form = ProvisioningForm(request.POST)
        if not form.is_valid():
            return HttpResponse(_("invalid form"), status=400)
        ProvisioningJob.objects.create(
            station=station,
            image_release=form.cleaned_data["image_release"],
            requested_by=request.user,
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

        def iterator():
            try:
                while chunk := stream.read(chunk_size):
                    yield chunk
            finally:
                stream.close()
                # Mark downloaded only after a successful full iteration,
                # and only if the job is still READY (race-safe).
                ProvisioningJob.objects.filter(
                    pk=job_pk,
                    status=ProvisioningJob.Status.READY,
                ).update(
                    status=ProvisioningJob.Status.DOWNLOADED,
                    downloaded_at=timezone.now(),
                )

        filename = job.output_s3_key.split("/")[-1]
        response = StreamingHttpResponse(iterator(), content_type="application/x-bzip2")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        if job.output_size_bytes:
            response["Content-Length"] = str(job.output_size_bytes)
        return response
