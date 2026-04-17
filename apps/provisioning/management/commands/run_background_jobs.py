from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.images import cosign, github
from apps.images import storage as image_storage
from apps.images.models import ImageImportJob, ImageRelease


class Command(BaseCommand):
    help = "Process queued image imports and provisioning jobs."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run continuously")
        parser.add_argument("--interval", type=int, default=5, help="Seconds between ticks")

    def handle(self, *args, **opts):
        while True:
            process_pending_image_imports()
            # process_pending_provisioning_jobs() — added in Phase 2
            # cleanup_expired_provisioning_outputs() — added in Phase 2
            if not opts["loop"]:
                return
            time.sleep(opts["interval"])


def process_pending_image_imports() -> None:
    repo = getattr(settings, "LINUX_IMAGE_REPO", "OE5XRX/linux-image")

    pending = ImageImportJob.objects.filter(status=ImageImportJob.Status.PENDING).order_by(
        "created_at"
    )

    for job in pending:
        job.status = ImageImportJob.Status.RUNNING
        job.save(update_fields=["status"])

        try:
            asset = github.fetch_release_asset(repo=repo, tag=job.tag, machine=job.machine)
            cosign.verify_blob(
                blob_bytes=asset.wic_bytes,
                bundle_bytes=asset.bundle_bytes,
                repo=repo,
                tag=job.tag,
            )
            wic_key = image_storage.release_key(job.tag, job.machine)
            bundle_key = image_storage.release_bundle_key(job.tag, job.machine)
            image_storage.upload_bytes(wic_key, asset.wic_bytes)
            image_storage.upload_bytes(bundle_key, asset.bundle_bytes)

            release = ImageRelease.objects.create(
                tag=job.tag,
                machine=job.machine,
                s3_key=wic_key,
                cosign_bundle_s3_key=bundle_key,
                sha256=asset.sha256,
                size_bytes=len(asset.wic_bytes),
                is_latest=job.mark_as_latest,
                imported_by=job.requested_by,
            )
            job.image_release = release
            job.status = ImageImportJob.Status.READY
            job.completed_at = timezone.now()
            job.save(update_fields=["image_release", "status", "completed_at"])
        except Exception as exc:
            job.status = ImageImportJob.Status.FAILED
            job.error_message = str(exc)
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "error_message", "completed_at"])
