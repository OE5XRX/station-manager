from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


class CosignVerificationError(RuntimeError):
    pass


COSIGN_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


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
    identity_regexp = (
        f"https://github.com/{re.escape(repo)}"
        f"/.github/workflows/release.yml@refs/tags/{re.escape(tag)}"
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
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise CosignVerificationError(
                f"cosign verify-blob failed: {result.stderr.decode('utf-8', 'replace')}"
            )
