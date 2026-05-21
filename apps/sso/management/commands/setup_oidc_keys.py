"""Bootstrap the RSA private key used to sign OIDC ID tokens.

Idempotent by design — re-running on a host that already has a key
must NOT regenerate it (that would invalidate every live token).
Pass --force only when an operator deliberately wants to rotate.

Rotation note: --force invalidates every currently-signed ID token.
RPs that cache the JWKS will fail signature verification until they
re-fetch — the access tokens themselves remain valid (DOT stores
them as opaque strings, not JWTs).

Concurrency note: this command assumes a single caller (run from a
deploy script before any worker starts). Two simultaneous callers
on a fresh host can both pass the existence check and the later
writer wins, silently invalidating any work the earlier signer
already did. Operationally, that's fine — but don't fan this out
to multiple workers.
"""

import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the RSA-2048 private key for OIDC ID-token signing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=None,
            help="Override the destination path (default: settings.OIDC_RSA_KEY_PATH).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate even if a key already exists at the target path.",
        )

    def handle(self, *args, path=None, force=False, **options):
        from django.conf import settings

        target = Path(path or settings.OIDC_RSA_KEY_PATH)
        if target.exists() and not force:
            self.stdout.write(f"Key already present at {target}; nothing to do.")
            return

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Atomic write: create tempfile with 0o600 from the start, write, then
        # replace. Closes the chmod-after-write TOCTOU window where the private
        # key would briefly be world-readable on a default-umask host.
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(target.parent), prefix=".private-", suffix=".pem"
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
            os.replace(tmp_path_str, target)  # atomic within same filesystem
        except BaseException:
            try:
                os.unlink(tmp_path_str)
            except FileNotFoundError:
                pass
            raise

        self.stdout.write(self.style.SUCCESS(f"Wrote RSA-2048 private key to {target}."))
