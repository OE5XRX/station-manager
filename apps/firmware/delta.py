import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)


def compute_delta(source_artifact, target_artifact):
    """Compute an xdelta3 delta between two firmware artifacts.

    Returns a FirmwareDelta instance or None if computation fails.
    Requires xdelta3 to be installed on the system.
    """
    from apps.firmware.models import FirmwareDelta

    if not shutil.which("xdelta3"):
        logger.error("xdelta3 is not installed or not found on PATH.")
        return None

    # Check if delta already exists
    existing = FirmwareDelta.objects.filter(
        source_artifact=source_artifact,
        target_artifact=target_artifact,
    ).first()
    if existing:
        logger.info(
            "Delta already exists for %s -> %s (pk=%d).",
            source_artifact,
            target_artifact,
            existing.pk,
        )
        return existing

    tmpdir = tempfile.mkdtemp(prefix="fwdelta_")
    try:
        source_path = Path(tmpdir) / "source.img"
        target_path = Path(tmpdir) / "target.img"
        delta_path = Path(tmpdir) / "delta.xdelta3"

        # Copy artifact files to temp dir
        with source_artifact.file.open("rb") as src:
            source_path.write_bytes(src.read())

        with target_artifact.file.open("rb") as tgt:
            target_path.write_bytes(tgt.read())

        # Run xdelta3
        result = subprocess.run(
            [
                "xdelta3",
                "-e",
                "-s",
                str(source_path),
                str(target_path),
                str(delta_path),
            ],
            capture_output=True,
            timeout=3600,  # 1 hour timeout for large images
        )

        if result.returncode != 0:
            logger.error(
                "xdelta3 failed (rc=%d) for %s -> %s: %s",
                result.returncode,
                source_artifact,
                target_artifact,
                result.stderr.decode(errors="replace"),
            )
            return None

        # Read delta and compute checksum
        delta_bytes = delta_path.read_bytes()
        delta_size = len(delta_bytes)
        checksum = hashlib.sha256(delta_bytes).hexdigest()

        # Build a filename for storage
        filename = (
            f"{target_artifact.name}_"
            f"{source_artifact.version}_to_{target_artifact.version}.xdelta3"
        )

        delta = FirmwareDelta(
            source_artifact=source_artifact,
            target_artifact=target_artifact,
            delta_size=delta_size,
            checksum_sha256=checksum,
        )
        delta.delta_file.save(filename, ContentFile(delta_bytes), save=False)
        delta.save()

        logger.info(
            "Delta computed: %s -> %s, size=%d bytes (%.1f%% of full image).",
            source_artifact,
            target_artifact,
            delta_size,
            (delta_size / target_artifact.file_size * 100) if target_artifact.file_size else 0,
        )
        return delta

    except subprocess.TimeoutExpired:
        logger.error(
            "xdelta3 timed out for %s -> %s.",
            source_artifact,
            target_artifact,
        )
        return None
    except Exception:
        logger.exception(
            "Unexpected error computing delta for %s -> %s.",
            source_artifact,
            target_artifact,
        )
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
