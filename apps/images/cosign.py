from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


class CosignVerificationError(RuntimeError):
    pass


COSIGN_OIDC_ISSUER = "https://token.actions.githubusercontent.com"

# cosign verify-blob on a ~70 MB asset completes in seconds locally. A minute
# is generous; past that we assume the subprocess is stuck and abort rather
# than let it pin the worker loop forever.
_COSIGN_TIMEOUT = 60  # seconds


def verify_blob(
    blob_bytes: bytes,
    bundle_bytes: bytes,
    repo: str,
    tag: str,
) -> None:
    """Verify a cosign-signed blob against its GitHub Actions OIDC identity.

    Raises:
        CosignVerificationError: if verification fails for any reason.
    """
    # Escape repo and tag so tag metacharacters (e.g. '.', '+') are treated
    # as literals and cannot widen the trusted identity to unrelated workflows.
    # Anchor with ^...$ and also escape the literal slashes/dots in the fixed
    # workflow URL — without anchors, a tag like "v1" would match an identity
    # for ".../refs/tags/v1-alpha".
    identity_regexp = (
        rf"^https://github\.com/{re.escape(repo)}"
        rf"/\.github/workflows/release\.yml@refs/tags/{re.escape(tag)}$"
    )
    with tempfile.TemporaryDirectory() as tmp:
        blob_path = Path(tmp) / "blob"
        bundle_path = Path(tmp) / "bundle"
        blob_path.write_bytes(blob_bytes)
        bundle_path.write_bytes(bundle_bytes)
        cmd = [
            "cosign",
            "verify-blob",
            "--bundle",
            str(bundle_path),
            "--certificate-identity-regexp",
            identity_regexp,
            "--certificate-oidc-issuer",
            COSIGN_OIDC_ISSUER,
            str(blob_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=_COSIGN_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise CosignVerificationError(
                f"cosign verify-blob timed out after {_COSIGN_TIMEOUT}s"
            ) from exc
        if result.returncode != 0:
            raise CosignVerificationError(
                f"cosign verify-blob failed: {result.stderr.decode('utf-8', 'replace')}"
            )
