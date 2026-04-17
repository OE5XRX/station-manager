import logging

from django.core.management.base import BaseCommand

from apps.firmware.delta import compute_delta
from apps.firmware.models import FirmwareArtifact, FirmwareDelta

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Compute xdelta3 deltas between consecutive stable firmware artifacts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Recompute deltas even if they already exist.",
        )
        parser.add_argument(
            "--artifact",
            type=int,
            default=None,
            help="Compute delta for a specific artifact (by primary key).",
        )

    def handle(self, *args, **options):
        force = options["force"]
        artifact_pk = options["artifact"]

        if artifact_pk:
            try:
                target = FirmwareArtifact.objects.get(pk=artifact_pk)
            except FirmwareArtifact.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"Artifact pk={artifact_pk} not found."))
                return
            targets = [target]
        else:
            targets = FirmwareArtifact.objects.filter(is_stable=True).order_by(
                "name", "created_at"
            )

        computed = 0
        skipped = 0
        failed = 0

        for target in targets:
            # Find the previous stable version of the same name
            source = (
                FirmwareArtifact.objects.filter(
                    name=target.name,
                    is_stable=True,
                    created_at__lt=target.created_at,
                )
                .order_by("-created_at")
                .first()
            )

            if source is None:
                self.stdout.write(f"  Skipping {target}: no previous stable version found.")
                skipped += 1
                continue

            # Check if delta already exists
            exists = FirmwareDelta.objects.filter(
                source_artifact=source,
                target_artifact=target,
            ).exists()

            if exists and not force:
                self.stdout.write(
                    f"  Skipping {source.version} -> {target.version}: delta exists."
                )
                skipped += 1
                continue

            if exists and force:
                self.stdout.write(
                    f"  Removing existing delta {source.version} -> {target.version} (--force)."
                )
                FirmwareDelta.objects.filter(
                    source_artifact=source,
                    target_artifact=target,
                ).delete()

            self.stdout.write(f"  Computing delta: {source} -> {target} ...")
            delta = compute_delta(source, target)

            if delta:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"    Done: {delta.delta_size_display} "
                        f"(sha256: {delta.checksum_sha256[:16]}...)"
                    )
                )
                computed += 1
            else:
                self.stderr.write(
                    self.style.ERROR(f"    Failed to compute delta for {source} -> {target}.")
                )
                failed += 1

        self.stdout.write(f"\nSummary: {computed} computed, {skipped} skipped, {failed} failed.")
