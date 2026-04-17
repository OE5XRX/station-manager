from __future__ import annotations

import bz2
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.api.models import DeviceKey
from apps.images import cosign, github
from apps.images import storage as image_storage
from apps.images.models import ImageImportJob, ImageRelease
from apps.provisioning import guestfish
from apps.provisioning.config_render import render_config
from apps.provisioning.models import ProvisioningJob


class Command(BaseCommand):
    help = "Process queued image imports and provisioning jobs."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run continuously")
        parser.add_argument("--interval", type=int, default=5, help="Seconds between ticks")

    def handle(self, *args, **opts):
        while True:
            process_pending_image_imports()
            process_pending_provisioning_jobs()
            cleanup_expired_provisioning_outputs()
            if not opts["loop"]:
                return
            time.sleep(opts["interval"])


def _claim_one_pending(model, status_field: str = "status"):
    """Atomically claim the oldest pending row.

    Returns the claimed instance (already transitioned to RUNNING) or None
    when no pending row exists. Concurrent workers see the gating UPDATE
    succeed for exactly one of them; losers retry and pick up a different
    candidate on the next iteration.
    """
    pending_status = model.Status.PENDING
    running_status = model.Status.RUNNING
    while True:
        candidate = (
            model.objects.filter(**{status_field: pending_status})
            .order_by("created_at")
            .values_list("pk", flat=True)
            .first()
        )
        if candidate is None:
            return None
        updated = model.objects.filter(pk=candidate, **{status_field: pending_status}).update(
            **{status_field: running_status}
        )
        if updated == 1:
            return model.objects.get(pk=candidate)
        # Race: another worker claimed this row; loop to find another.


def process_pending_image_imports() -> None:
    while (job := _claim_one_pending(ImageImportJob)) is not None:
        _run_import_job(job)


def _run_import_job(job: ImageImportJob) -> None:
    repo = getattr(settings, "LINUX_IMAGE_REPO", "OE5XRX/linux-image")
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

        try:
            release, _created = ImageRelease.objects.update_or_create(
                tag=job.tag,
                machine=job.machine,
                defaults={
                    "s3_key": wic_key,
                    "cosign_bundle_s3_key": bundle_key,
                    "sha256": asset.sha256,
                    "size_bytes": len(asset.wic_bytes),
                    "is_latest": job.mark_as_latest,
                    "imported_by": job.requested_by,
                },
            )
            job.image_release = release
            job.status = ImageImportJob.Status.READY
            job.completed_at = timezone.now()
            job.save(update_fields=["image_release", "status", "completed_at"])
        except Exception:
            # Don't leave orphan S3 objects if the DB write fails.
            # Cleanup is best-effort; re-raise the original exception so the
            # outer handler marks the job FAILED with the real error.
            for key in (wic_key, bundle_key):
                try:
                    image_storage.delete(key)
                except Exception:
                    pass
            raise
    except Exception as exc:
        job.status = ImageImportJob.Status.FAILED
        job.error_message = str(exc)
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])


PROVISIONING_EXPIRY = timedelta(hours=1)


def _decompress_to(src_path: Path, dst_path: Path) -> None:
    with bz2.open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)


def _compress_to_bytes(src_path: Path) -> bytes:
    # Stream the wic through BZ2Compressor in 1 MiB chunks so peak memory is
    # bounded to chunk size + compressor state (~1 MiB) regardless of input
    # size. The final join materializes the compressed output (~70 MiB for a
    # real wic) which is an order of magnitude smaller than the input.
    compressor = bz2.BZ2Compressor(compresslevel=9)
    chunks: list[bytes] = []
    with open(src_path, "rb") as src:
        while chunk := src.read(1 << 20):
            out = compressor.compress(chunk)
            if out:
                chunks.append(out)
    tail = compressor.flush()
    if tail:
        chunks.append(tail)
    return b"".join(chunks)


def _provisioning_output_key(job: ProvisioningJob) -> str:
    tag = job.image_release.tag
    machine = job.image_release.machine
    return f"provisioning/{job.id}/oe5xrx-station-{job.station_id}-{machine}-{tag}.wic.bz2"


def process_pending_provisioning_jobs() -> None:
    while (job := _claim_one_pending(ProvisioningJob)) is not None:
        _run_provisioning_job(job)


def _run_provisioning_job(job: ProvisioningJob) -> None:
    server_url = getattr(settings, "SERVER_PUBLIC_URL", "https://ham.oe5xrx.org")

    try:
        # Generate the keypair in memory; we will only persist the new
        # public half to DeviceKey after the bundle is successfully uploaded
        # so a failure in between does not invalidate the station's live key.
        private_pem, public_b64 = DeviceKey.generate_keypair()

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            compressed_in = tmp / "base.wic.bz2"
            decompressed = tmp / "work.wic"

            with (
                image_storage.open_stream(job.image_release.s3_key) as src,
                open(compressed_in, "wb") as dst,
            ):
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    dst.write(chunk)

            _decompress_to(compressed_in, decompressed)

            guestfish.inject_provisioning_files(
                wic_path=decompressed,
                partition_device=guestfish.data_partition_for(job.image_release.machine),
                config_yaml=render_config(
                    server_url=server_url,
                    station_id=job.station_id,
                ),
                private_key_pem=private_pem,
            )

            out_bytes = _compress_to_bytes(decompressed)

        out_key = _provisioning_output_key(job)
        image_storage.upload_bytes(out_key, out_bytes)

        try:
            # Bundle is safely in S3. Only now rotate the DeviceKey and flip
            # the job to READY — if anything above raised, the station's
            # existing key remains the authoritative one.
            DeviceKey.objects.update_or_create(
                station=job.station,
                defaults={
                    "current_public_key": public_b64,
                    "is_active": True,
                    "next_public_key": None,
                },
            )

            now = timezone.now()
            job.output_s3_key = out_key
            job.output_size_bytes = len(out_bytes)
            job.status = ProvisioningJob.Status.READY
            job.ready_at = now
            job.expires_at = now + PROVISIONING_EXPIRY
            job.save(
                update_fields=[
                    "output_s3_key",
                    "output_size_bytes",
                    "status",
                    "ready_at",
                    "expires_at",
                ]
            )
        except Exception:
            # Don't leave the uploaded bundle stranded in S3 if we can't
            # record it against the job. Cleanup is best-effort; re-raise
            # so the outer handler marks the job FAILED with the real error.
            try:
                image_storage.delete(out_key)
            except Exception:
                pass
            raise
    except Exception as exc:
        job.status = ProvisioningJob.Status.FAILED
        job.error_message = str(exc)
        job.save(update_fields=["status", "error_message"])


def cleanup_expired_provisioning_outputs() -> None:
    now = timezone.now()

    # Downloaded files — delete the S3 object once.
    downloaded = ProvisioningJob.objects.filter(
        status=ProvisioningJob.Status.DOWNLOADED,
    ).exclude(output_s3_key="")
    for job in downloaded:
        try:
            image_storage.delete(job.output_s3_key)
        except Exception:
            # Best-effort cleanup — leave output_s3_key intact so the next
            # tick retries. A transient S3 failure must not crash the loop.
            continue
        ProvisioningJob.objects.filter(pk=job.pk).update(output_s3_key="")

    # Expired before download.
    stale = ProvisioningJob.objects.filter(
        status=ProvisioningJob.Status.READY,
        expires_at__lt=now,
    )
    for job in stale:
        if job.output_s3_key:
            try:
                image_storage.delete(job.output_s3_key)
            except Exception:
                # Best-effort cleanup — retry on the next tick.
                continue
        job.status = ProvisioningJob.Status.EXPIRED
        job.output_s3_key = ""
        job.save(update_fields=["status", "output_s3_key"])
